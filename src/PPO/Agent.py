import tensorflow as tf
from tensorflow.keras import Model
from tensorflow.keras.layers import Conv2D, Dense, Flatten, Concatenate, Input, AvgPool2D
import numpy as np

class PPOAgentParams:
    def __init__(self):
        # Convolutional part config
        self.conv_layers = 2
        self.conv_kernel_size = 5
        self.conv_kernels = 16

        # Fully Connected config
        self.hidden_layer_size = 256
        self.hidden_layer_num = 3

        # Training Params
        self.learning_rate = 3e-5
        self.alpha = 0.005
        self.gamma = 0.95

        # Exploration strategy
        self.soft_max_scaling = 0.1

        # Global-Local Map
        self.global_map_scaling = 3
        self.local_map_size = 17

        # Scalar inputs instead of map
        self.use_scalar_input = False
        self.relative_scalars = False
        self.blind_agent = False
        self.max_uavs = 3
        self.max_devices = 10

        # Printing
        self.print_summary = False


class PPOAgent(object):
    def __init__(self, params: PPOAgentParams, example_state, example_action, stats=None):
        self.params = params
        self.learning_rate =self.params.learning_rate
        self.gamma = tf.constant(self.params.gamma, dtype=float)
        self.boolean_map_shape = example_state.get_boolean_map_shape() #get the environment map shape
        self.float_map_shape = example_state.get_float_map_shape() # get the device data map
        self.scalars = example_state.get_num_scalars(give_position=self.params.use_scalar_input) #get the movement budget size
        self.num_actions = len(type(example_action)) # 1


        # ---------------------------------------------------------------------
        # Create shared inputs
        action_input = Input(shape=(), name='action_input', dtype=tf.int64)
        reward_input = Input(shape=(), name='reward_input', dtype=tf.float32)
        termination_input = Input(shape=(), name='termination_input', dtype=tf.bool)
        #cumulative_rewards_input=Input(shape=(), name='cumulative_rewards_input', dtype=tf.float32)
        advantage_input=Input(shape=(), name='advantage_input', dtype=tf.float32)
        return_input = Input(shape=(), name='return_input', dtype=tf.float32)
        value_input = Input(shape=(), name='value_input', dtype=tf.float32)
        logprobability_input = Input(shape=(), name='logprobability_input', dtype=tf.float32)

        policy_learning_rate=self.params.policy_learning_rate
        value_function_learning_rate=self.params.value_function_learning_rate

        boolean_map_input = Input(shape=self.boolean_map_shape, name='boolean_map_input', dtype=tf.bool)
        float_map_input = Input(shape=self.float_map_shape, name='float_map_input', dtype=tf.float32)
        scalars_input = Input(shape=(self.scalars,), name='scalars_input', dtype=tf.float32)
        states = [boolean_map_input,float_map_input,scalars_input]

        map_cast = tf.cast(boolean_map_input, dtype=tf.float32) #张量数据类型转换
        padded_map = tf.concat([map_cast, float_map_input], axis=3) # tensors combine in one dimension


        #build the policy gradient network
        self.actor_network = self.build_actor_model(padded_map, scalars_input, states,'actor_model')
        self.critic_network = self.build_critic_model(padded_map, scalars_input, states, 'critic_model')


        self.policy_optimizer = tf.keras.optimizers.Adam(learning_rate=policy_learning_rate)
        self.value_optimizer = tf.keras.optimizers.Adam(learning_rate=value_function_learning_rate)
        action_probs=self.policy_network.output
        self.soft_explore_model = Model(inputs=states, outputs=action_probs)

        actions_one_hot = tf.one_hot(action_input, depth=self.num_actions, on_value=1.0, off_value=0.0, dtype=float)
        # actions_one_hot = tf.one_hot(current_action, self.policy_network.output_shape[1])
        log_probs =tf.math.log(tf.reduce_sum(tf.multiply(action_probs, actions_one_hot,name='mul_hot'), axis=1, name='log_probs'))
        loss = -tf.reduce_mean(log_probs * cumulative_rewards_input)
        self.loss_model = Model(
            inputs=states + [action_input, termination_input, cumulative_rewards_input],
            outputs=loss)



        self.global_map_model = Model(inputs=[boolean_map_input, float_map_input], outputs=self.global_map)
        self.local_map_model = Model(inputs=[boolean_map_input, float_map_input], outputs=self.local_map)
        self.total_map_model = Model(inputs=[boolean_map_input, float_map_input], outputs=self.total_map)


        if self.params.print_summary:
            self.loss_model.summary()
        if stats:
            stats.set_model(self.policy_network)




    def create_map_proc(self, conv_in, name): #parameter is the total map
        # Forking for global and local map
        # Global Map
        global_map = tf.stop_gradient(
            AvgPool2D((self.params.global_map_scaling, self.params.global_map_scaling))(conv_in))
        self.global_map = global_map # is the total map been poolled
        self.total_map = conv_in
        for k in range(self.params.conv_layers):
            global_map = Conv2D(self.params.conv_kernels, self.params.conv_kernel_size, activation='relu',
                                strides=(1, 1),
                                name=name + 'global_conv_' + str(k + 1))(global_map) #pass two conv layers
        flatten_global = Flatten(name=name + 'global_flatten')(global_map) #flat the whole map

        # Local Map
        crop_frac = float(self.params.local_map_size) / float(self.boolean_map_shape[0])
        local_map = tf.stop_gradient(tf.image.central_crop(conv_in, crop_frac))
        self.local_map = local_map
        for k in range(self.params.conv_layers):
            local_map = Conv2D(self.params.conv_kernels, self.params.conv_kernel_size, activation='relu',
                               strides=(1, 1),
                               name=name + 'local_conv_' + str(k + 1))(local_map)
        flatten_local = Flatten(name=name + 'local_flatten')(local_map)
        return Concatenate(name=name + 'concat_flatten')([flatten_global, flatten_local])


    def build_actor_model(self, map_proc, states_proc, inputs, name=''):
        flatten_map = self.create_map_proc(map_proc, name) #return the flatten local and environment map
        layer = Concatenate(name=name + 'concat')([flatten_map, states_proc])
        for k in range(self.params.hidden_layer_num):
            layer = Dense(self.params.hidden_layer_size, activation='relu', name=name + 'hidden_layer_all_' + str(k))(layer)
        output = Dense(self.num_actions, activation='linear', name=name + 'output_layer')(layer)
        action_probs = tf.keras.activations.softmax(output)
        action_probs = tf.clip_by_value(action_probs, 1e-8, 1 - 1e-8)
        model = Model(inputs=inputs, outputs=action_probs)
        return model

    def build_critic_model(self, map_proc, states_proc, inputs, name=''):
        flatten_map = self.create_map_proc(map_proc, name)  # return the flatten local and environment map
        layer = Concatenate(name=name + 'concat')([flatten_map, states_proc])
        for k in range(self.params.hidden_layer_num):
            layer = Dense(self.params.hidden_layer_size, activation='relu', name=name + 'hidden_layer_all_' + str(k))(
                layer)
        output = Dense(1, activation='linear', name=name + 'output_layer')(layer)
        model = Model(inputs=inputs, outputs=output)
        return model


    def get_random_action(self):
        return np.random.randint(0, self.num_actions)

    def get_action(self, state): # get the action base on the possiblity
        boolean_map_in = state.get_boolean_map()[tf.newaxis, ...]
        float_map_in = state.get_float_map()[tf.newaxis, ...]
        scalars = np.array(state.get_scalars(), dtype=np.single)[tf.newaxis, ...]
        p = self.soft_explore_model([boolean_map_in, float_map_in, scalars]).numpy()[0]
        return np.random.choice(range(self.num_actions), size=1, p=p)

    def get_exploitation_action_target(self, state):
        return self.get_action(state)

    def act(self, state):
        return self.get_action(state)



    def train_step(self,batch_bool_maps,batch_float_maps,batch_scalars, actions, terminated, cumulative_rewards):
        with tf.GradientTape() as tape:
            # bool_maps=tf.convert_to_tensor(batch_bool_maps)
            # actions = tf.convert_to_tensor(actions)
            # cumulative_rewards = tf.convert_to_tensor(cumulative_rewards)
            loss = self.loss_model(
                [batch_bool_maps,batch_float_maps,batch_scalars, actions,
                 terminated, cumulative_rewards])
        gradients = tape.gradient(loss, self.policy_network.trainable_variables)
        self.optimizer.apply_gradients(zip(gradients, self.policy_network.trainable_variables))



    def train(self,bool_maplist, float_maplist, scalarslist,actionslist,rewardslist,terminationslist,batch_size):
        G = 0
        Gs = []
        # boolean_map_inputs =[]
        # float_map_inputs = []
        # scalars_inputs = []


        for r in rewardslist[::-1]:
            G = r + self.gamma * G
            Gs.insert(0, G)
        Gs = np.array(Gs)
        Gs = (Gs - np.mean(Gs)) / (np.std(Gs) + 1e-9)
        cumulative_rewards = np.array(Gs, dtype=np.float32)

        # for current_state in stateslist:
        #     boolean_map_inputs.append(current_state[0])
        #     float_map_inputs.append(current_state[1])
        #     scalars_inputs.append(current_state[2])
        #states = np.array(stateslist)
        #states=states.astype(float)
        bool_map=np.array(bool_maplist)
        float_map=np.array(float_maplist)
        scalar=np.array(scalarslist)
        actions = np.array(actionslist)
        #actions=actions.astype(int)
        terminations=np.array(terminationslist)

        for i in range(0, len(actions), batch_size):
            # batch_boolean_map_inputs=boolean_map_inputs[i:i + batch_size]
            # batch_float_map_inputs=float_map_inputs[i:i + batch_size]
            # batch_scalars_inputs=scalars_inputs[i:i + batch_size]
            #batch_states = states[i:i + batch_size]
            batch_bool_maps=bool_map[i:i + batch_size]
            batch_float_maps=float_map[i:i + batch_size]
            batch_scalars=scalar[i:i + batch_size]

            batch_actions = actions[i:i + batch_size]
            #batch_map_casts = tf.cast(batch_boolean_map_inputs, dtype=tf.float32)
            #batch_padded_maps = tf.concat([batch_map_casts, batch_float_map_inputs], axis=3)
            batch_rewards=cumulative_rewards[i:i + batch_size]
            batch_terminated=terminations[i:i + batch_size]
            self.train_step(batch_bool_maps,batch_float_maps,batch_scalars, batch_actions, batch_terminated,batch_rewards)


    def get_global_map(self, state):
        boolean_map_in = state.get_boolean_map()[tf.newaxis, ...]
        float_map_in = state.get_float_map()[tf.newaxis, ...]
        return self.global_map_model([boolean_map_in, float_map_in]).numpy()

    def get_local_map(self, state):
        boolean_map_in = state.get_boolean_map()[tf.newaxis, ...]
        float_map_in = state.get_float_map()[tf.newaxis, ...]
        return self.local_map_model([boolean_map_in, float_map_in]).numpy()

    def get_total_map(self, state):
        boolean_map_in = state.get_boolean_map()[tf.newaxis, ...]
        float_map_in = state.get_float_map()[tf.newaxis, ...]
        return self.total_map_model([boolean_map_in, float_map_in]).numpy()
