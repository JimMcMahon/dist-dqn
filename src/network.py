from functools import partial
from six.moves import zip

import tensorflow as tf

# Base-class for the Deep Q-Network architecture. Constructs the TensorFlow
# graph with layers, weights, biases, loss-function, optimizer, etc. for
# a network of given type. Currently, a simple network with two hidden layers,
# and a convolutional neural-network are support.
#
# New network architectures can be added by sub-classing Network and
# implmementing the _init_params() and _init_layers() methods.
class Network:
  x_placeholder = None
  q_placeholder = None
  action_placeholder = None

  q_output = None
  train_op = None

  target_q_output = None
  target_update_ops = None

  summary_op = None
  global_step = None

  def __init__(self, input_shape, num_actions, num_replicas=1,
               ps_device=None, worker_device=None):
    self.input_shape = list(input_shape)
    self.num_actions = num_actions
    self.num_replicas = num_replicas # Used for synchronous training if enabled
    self.ps_device = ps_device # Device constraints used by param server
    self.worker_device = worker_device # Used for target param replication

  @staticmethod
  def create_network(config, input_shape, num_actions, num_replicas=1,
                     ps_device=None, worker_device=None):
    """
    Creates and returns a network type based on config.network.
    """
    Net = {
      'simple': SimpleNetwork,
      'cnn': ConvNetwork,
    }.get(config.network, None)

    if Net is None:
      raise RuntimeError('Unsupported network type {}'.format(config.network))

    net = Net(
      input_shape=input_shape,
      num_actions=num_actions,
      num_replicas=num_replicas,
      ps_device=ps_device,
      worker_device=worker_device,
    )
    net._init_network(config)
    return net
  
  def _init_network(self, config):
    # Placeholders
    self.x_placeholder = tf.placeholder(tf.float32, [None] + self.input_shape)
    self.q_placeholder = tf.placeholder(tf.float32, [None])
    self.action_placeholder = tf.placeholder(tf.float32, 
                                             [None, self.num_actions])

    summaries = []

    # Params and layers
    with tf.device(self.ps_device):
      params = self._init_params(
        config,
        input_shape=self.input_shape,
        output_size=self.num_actions,
        summaries=summaries,
      )
    self.q_output, reg_loss = self._init_layers(
      config,
      inputs=self.x_placeholder,
      params=params,
      summaries=summaries,
    )

    # Loss and training
    self.global_step = tf.Variable(0, name='global_step', trainable=False)
    loss = self._init_loss(
      config,
      q=self.q_output,
      expected_q=self.q_placeholder,
      actions=self.action_placeholder,
      reg_loss=reg_loss,
      summaries=summaries,
    )
    self.train_op = self._init_optimizer(
      config,
      params=params,
      loss=loss,
      num_replicas=self.num_replicas,
      global_step=self.global_step,
      summaries=summaries,
    )

    # Target network
    self.target_q_output, self.target_update_ops = self._init_target_network(
      config,
      inputs=self.x_placeholder,
      input_shape=self.input_shape,
      output_size=self.num_actions,
      params=params,
      ps_device=self.ps_device,
      worker_device=self.worker_device,
      summaries=summaries,
    )

    # Merge all the summaries in this graph
    if summaries:
      self.summary_op = tf.summary.merge(summaries)

  @classmethod
  def _init_params(cls, config, input_shape, output_size, summaries=None):
    """
    Setup the trainable params for the network. Subclasses should
    implement this to create all the weights and biases.

    @return: Tuple of weights and biases
    """
    raise NotImplementedError

  @classmethod
  def _init_layers(cls, config, inputs, params, summaries=None):
    """
    Setup the layers and trainable params of the network. Subclasses should
    implement this to initialize the appropriate network architecture.

    @param inputs: Placeholder for the input layer
    @param params: Tuple of weights and biases returned by _init_params()

    @return: (output_layer, regularized_loss)
    """
    raise NotImplementedError

  @classmethod
  def _init_loss(cls, config, q, expected_q, actions, reg_loss=None,
                 summaries=None):
    """
    Setup the loss function and apply regularization is provided.

    @return: loss_op
    """
    q_masked = tf.reduce_sum(tf.multiply(q, actions), reduction_indices=[1])
    loss = tf.reduce_mean(tf.squared_difference(q_masked, expected_q))
    if reg_loss is not None:
      loss += config.reg_param * reg_loss

    if summaries is not None:
      summaries.append(tf.summary.scalar('loss', loss))

    return loss

  @classmethod
  def _init_optimizer(cls, config, params, loss, num_replicas=1,
                      global_step=None, summaries=None):
    """
    Setup the optimizer for the provided params based on the loss function.
    Relies on config.optimizer to select the type of optimizer.

    @return: train_op
    """

    Optimizer = {
      'adadelta': tf.train.AdadeltaOptimizer,
      'adagrad': tf.train.AdagradOptimizer,
      'adam': tf.train.AdamOptimizer,
      'ftrl': tf.train.FtrlOptimizer,
      'sgd': tf.train.GradientDescentOptimizer,
      'momentum': partial(tf.train.MomentumOptimizer, momentum=config.momentum),
      'rmsprop': partial(tf.train.RMSPropOptimizer, decay=config.rmsprop_decay),
    }.get(config.optimizer, None)

    if Optimizer is None:
      raise RuntimeError('Unsupported optimizer {}'.format(config.optimizer))

    # TODO: Experiment with gating gradients for improved parallelism
    # https://www.tensorflow.org/versions/r0.9/api_docs/python/train.html#gating-gradients
    optimizer = Optimizer(learning_rate=config.lr)

    # Synchronize gradient updates if enabled
    if config.sync:
      optimizer = tf.train.SyncReplicasOptimizer(
        optimizer,
        replicas_to_aggregate=num_replicas,
        replica_id=config.task_id,
      )

    # Explicitly pass the list of trainable params instead of defaulting to
    # GraphKeys.TRAINABLE_VARIABLES. Otherwise, when this network becomes a
    # subgraph when in-graph replication is configured, TRAINABLE_VARIABLES
    # will contain params from all graph replicas due to global namespacing.
    train_op = optimizer.minimize(
      loss,
      var_list=params,
      global_step=global_step,
    )
    return train_op

  def _init_target_network(cls, config, inputs, input_shape, output_size,
                           params, ps_device=None, worker_device=None,
                           summaries=None):
    """
    Setup the target network used for minibatch training, and the
    update operations to periodically update the target network with
    the trained network.

    @return: target_q_output, [target_update_ops]
    """

    if not config.disable_target_replication:
      # Replicate the target network params within each worker instead of it
      # being managed by the param server. Since the target network is frozen
      # for many steps, this cuts down the communication overhead of
      # transferring them from the param server's device during each train loop.
      # Also, they need to be marked as local variables so that all workers
      # initialize them locally. Otherwise, non-chief workers are forever
      # waiting for the chief worker to initialize the replicated target params.
      target_param_device = worker_device
      collections = [tf.GraphKeys.LOCAL_VARIABLES]
    else:
      # If target param replication is disabled, param server takes the
      # ownership of target params. Allocate on the same device as the other
      # params managed by the param server.
      target_param_device = ps_device
      collections = []

    # Initialize the target weights and layers
    with tf.variable_scope('target'):
      with tf.device(target_param_device):
        target_params = cls._init_params(
          config,
          input_shape=input_shape,
          output_size=output_size,
          collections=collections,
          summaries=summaries,
        )
      target_q_output, _ = cls._init_layers(
        config,
        inputs=inputs,
        params=target_params,
        summaries=summaries,
      )

    # Create assign ops to periodically update the target network
    target_update_ops = \
      [tf.assign(target_p, p) for target_p, p in zip(target_params, params)]

    return target_q_output, target_update_ops

# Simple fully connected network with two fully connected layers with
# tanh activations and a final Affine layer.
class SimpleNetwork(Network):
  HIDDEN1_SIZE = 20
  HIDDEN2_SIZE = 20

  @classmethod
  def _init_params(cls, config, input_shape, output_size, collections=None,
                   summaries=None):
    if len(input_shape) != 1:
      raise RuntimeError('%s expects 1-d input' % cls.__name__)
    input_size = input_shape[0]

    weight_init = tf.truncated_normal_initializer(stddev=0.01)
    bias_init = tf.constant_initializer(value=0.0)

    # First hidden layer
    with tf.variable_scope('hidden1'):
      shape = [input_size, cls.HIDDEN1_SIZE]
      w1 = tf.get_variable('w', shape, initializer=weight_init,
                           collections=collections)
      b1 = tf.get_variable('b', cls.HIDDEN1_SIZE, initializer=bias_init,
                           collections=collections)

    # Second hidden layer
    with tf.variable_scope('hidden2'):
      shape = [cls.HIDDEN1_SIZE, cls.HIDDEN2_SIZE]
      w2 = tf.get_variable('w', shape, initializer=weight_init,
                           collections=collections)
      b2 = tf.get_variable('b', cls.HIDDEN2_SIZE, initializer=bias_init,
                           collections=collections)

    # Output layer
    with tf.variable_scope('output'):
      shape = [cls.HIDDEN2_SIZE, output_size]
      w3 = tf.get_variable('w', shape, initializer=weight_init,
                           collections=collections)
      b3 = tf.get_variable('b', output_size, initializer=bias_init,
                           collections=collections)

    return (w1, b1, w2, b2, w3, b3)

  @classmethod
  def _init_layers(cls, config, inputs, params, summaries=None):
    w1, b1, w2, b2, w3, b3 = params

    # Layers
    with tf.name_scope('hidden1'):
      a1 = tf.nn.tanh(tf.matmul(inputs, w1) + b1, name='tanh')
    with tf.name_scope('hidden2'):
      a2 = tf.nn.tanh(tf.matmul(a1, w2) + b2, name='tanh')
    with tf.name_scope('output'):
      output = tf.add(tf.matmul(a2, w3), b3, name='affine')

    # L2 regularization for weights excluding biases
    reg_loss = sum(tf.nn.l2_loss(w) for w in [w1, w2, w3])

    return output, reg_loss

# Convolutional network described in
# https://storage.googleapis.com/deepmind-data/assets/papers/DeepMindNature14236Paper.pdf
class ConvNetwork(Network):
  CONV1_FILTERS = 32
  CONV1_SIZE = 8
  CONV1_STRIDE = 4

  CONV2_FILTERS = 64
  CONV2_SIZE = 4
  CONV2_STRIDE = 2

  CONV3_FILTERS = 64
  CONV3_SIZE = 3
  CONV3_STRIDE = 1

  POOL_SIZE = [1, 2, 2, 1]
  POOL_STRIDE = [1, 2, 2, 1]

  FULLY_CONNECTED_SIZE = 256

  @classmethod
  def _init_params(cls, config, input_shape, output_size, collections=None,
                   summaries=None):
    if len(input_shape) != 3:
      raise RuntimeError('%s expects 3-d input' % cls.__class__.__name__)

    weight_init = tf.truncated_normal_initializer(stddev=0.01)
    bias_init = tf.constant_initializer(value=0.0)

    # First hidden conv-pool layer
    with tf.variable_scope('conv1'):
      shape = \
        [cls.CONV1_SIZE, cls.CONV1_SIZE, input_shape[2], cls.CONV1_FILTERS]
      w1 = tf.get_variable('w', shape, initializer=weight_init,
                           collections=collections)
      b1 = tf.get_variable('b', cls.CONV1_FILTERS, initializer=bias_init,
                           collections=collections)

    # Second hidden conv-pool layer
    with tf.variable_scope('conv2'):
      shape = \
        [cls.CONV2_SIZE, cls.CONV2_SIZE, cls.CONV1_FILTERS, cls.CONV2_FILTERS]
      w2 = tf.get_variable('w', shape, initializer=weight_init,
                           collections=collections)
      b2 = tf.get_variable('b', cls.CONV2_FILTERS, initializer=bias_init,
                           collections=collections)

    # Third hidden conv-pool layer
    with tf.variable_scope('conv3'):
      shape = \
        [cls.CONV3_SIZE, cls.CONV3_SIZE, cls.CONV2_FILTERS, cls.CONV3_FILTERS]
      w3 = tf.get_variable('w', shape, initializer=weight_init,
                           collections=collections)
      b3 = tf.get_variable('b', cls.CONV3_FILTERS, initializer=bias_init,
                           collections=collections)

    # Final fully-connected hidden layer
    with tf.variable_scope('fcl'):
      shape = [cls.FULLY_CONNECTED_SIZE, cls.FULLY_CONNECTED_SIZE]
      w4 = tf.get_variable('w', shape, initializer=weight_init,
                           collections=collections)
      b4 = tf.get_variable('b', cls.FULLY_CONNECTED_SIZE, initializer=bias_init,
                           collections=collections)

    # Output layer
    with tf.variable_scope('output'):
      shape = [cls.FULLY_CONNECTED_SIZE, output_size]
      w5 = tf.get_variable('w', shape, initializer=weight_init,
                           collections=collections)
      b5 = tf.get_variable('b', output_size, initializer=bias_init,
                           collections=collections)

    return (w1, b1, w2, b2, w3, b3, w4, b4, w5, b5)

  @classmethod
  def _init_layers(cls, config, inputs, params, summaries=None):
    w1, b1, w2, b2, w3, b3, w4, b4, w5, b5 = params

    # Layers
    with tf.name_scope('conv1'):
      a1 = cls.conv_pool(inputs, w1, b1, cls.CONV1_STRIDE)
    with tf.name_scope('conv2'):
      a2 = cls.conv_pool(a1, w2, b2, cls.CONV2_STRIDE)
    with tf.name_scope('conv3'):
      a3 = cls.conv_pool(a2, w3, b3, cls.CONV3_STRIDE)
    with tf.name_scope('fcl'):
      a3_flat = tf.reshape(a3, [-1, cls.FULLY_CONNECTED_SIZE])
      a4 = tf.nn.relu(tf.matmul(a3_flat, w4) + b4, name='relu')
    with tf.name_scope('output'):
      output = tf.add(tf.matmul(a4, w5), b5, name='affine')

    # L2 regularization for fully-connected weights
    reg_loss = sum(tf.nn.l2_loss(w) for w in [w4, w5])

    return output, reg_loss

  @classmethod
  def conv_stride(cls, stride):
    return [1, stride, stride, 1]

  @classmethod
  def conv_pool(cls, inputs, filters, bias, stride):
    conv = tf.nn.conv2d(inputs, filters, strides=cls.conv_stride(stride),
                        padding='SAME', name='conv')
    return cls.max_pool(tf.nn.relu(conv + bias))

  @classmethod
  def max_pool(cls, a):
    return tf.nn.max_pool(a, ksize=cls.POOL_SIZE, strides=cls.POOL_STRIDE,
                          padding='SAME', name='pool')
