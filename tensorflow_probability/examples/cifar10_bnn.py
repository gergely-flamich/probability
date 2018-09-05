# Copyright 2018 The TensorFlow Probability Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Trains a Bayesian neural network to classify MNIST digits.

The architecture is LeNet-5 [1].

#### References

[1]: Yann LeCun, Leon Bottou, Yoshua Bengio, and Patrick Haffner.
     Gradient-based learning applied to document recognition.
     _Proceedings of the IEEE_, 1998.
     http://yann.lecun.com/exdb/publis/pdf/lecun-01a.pdf
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os

# Dependency imports
from absl import flags
import matplotlib
matplotlib.use("Agg")
#from matplotlib import figure  # pylint: disable=g-import-not-at-top
#from matplotlib.backends import backend_agg
import numpy as np
import tensorflow as tf
#import tensorflow_probability as tfp

from tensorflow.python.keras.datasets import cifar10

import seaborn as sns  # pylint: disable=g-import-not-at-top
from models.BResNet import BayesResNet
from models.BVGG import BVGG

# TODO(b/78137893): Integration tests currently fail with seaborn imports.
import warnings
warnings.simplefilter(action='ignore')

#try:
#  import seaborn as sns  # pylint: disable=g-import-not-at-top
#  HAS_SEABORN = True
#except ImportError:
#  HAS_SEABORN = False

tfd = tf.contrib.distributions

IMAGE_SHAPE = [32, 32, 3]

flags.DEFINE_float("learning_rate",
                   default=0.0001,
                   help="Initial learning rate.")
flags.DEFINE_integer("epochs",
                     default=700,
                     help="Number of epochs to train for.")
flags.DEFINE_integer("batch_size",
                     default=128,
                     help="Batch size.")
flags.DEFINE_string("data_dir",
                    default=os.path.join(os.getenv("TEST_TMPDIR", "/tmp"),
                                         "bayesian_neural_network/data"),
                    help="Directory where data is stored (if using real data).")
flags.DEFINE_string(
    "model_dir",
    default=os.path.join(os.getenv("TEST_TMPDIR", "/tmp"),
                         "bayesian_neural_network/"),
    help="Directory to put the model's fit.")
flags.DEFINE_integer("eval_freq",
                     default=400,
                     help="Frequency at which to validate the model.")
flags.DEFINE_integer("num_monte_carlo",
                     default=50,
                     help="Network draws to compute predictive probabilities.")
flags.DEFINE_string("arch",
                  default="vgg",
                  help="Network architecture to use.")
flags.DEFINE_integer("prior_qmean",
                     default=-9,
                     help="Initial posterior mean of the log variance for q(w)")
flags.DEFINE_float("variance_threshold",
                     default=0.2,
                     help="Posterior variance threshold constraint. Log var <= log(threshold).")
flags.DEFINE_integer("kl_annealing",
                     default=100,
                     help="Epochs to anneal the KL term (anneals from 0 to 1)")

FLAGS = flags.FLAGS

def build_input_pipeline(x_train, x_test, y_train, y_test, 
                         batch_size, valid_size):
  """Build an Iterator switching between train and heldout data."""

  x_train = x_train.astype('float32')
  x_test = x_test.astype('float32')

  x_train /= 255
  x_test /= 255

  y_train = y_train.flatten()
  y_test = y_test.flatten()

  print('x_train shape:' + str(x_train.shape))
  print(str(x_train.shape[0]) + ' train samples')
  print(str(x_test.shape[0]) + ' test samples')

  # Build an iterator over training batches.
  training_dataset = tf.data.Dataset.from_tensor_slices(
      (x_train, np.int32(y_train)))
  training_batches = training_dataset.shuffle(50000, 
                                              reshuffle_each_iteration=True).repeat().batch(batch_size)
  training_iterator = training_batches.make_one_shot_iterator()

  # Build a iterator over the heldout set with batch_size=heldout_size,
  # i.e., return the entire heldout set as a constant.
  heldout_dataset = tf.data.Dataset.from_tensor_slices(
      (x_test, np.int32(y_test)))
  heldout_batches = heldout_dataset.repeat().batch(valid_size)
  heldout_iterator = heldout_batches.make_one_shot_iterator()

  # Combine these into a feedable iterator that can switch between training
  # and validation inputs.
  handle = tf.placeholder(tf.string, shape=[])
  feedable_iterator = tf.data.Iterator.from_string_handle(
      handle, training_batches.output_types, training_batches.output_shapes)
  images, labels = feedable_iterator.get_next()

  return images, labels, handle, training_iterator, heldout_iterator


def main(argv):
  del argv  # unused
  if tf.gfile.Exists(FLAGS.model_dir):
    tf.logging.warning(
        "Warning: deleting old log directory at {}".format(FLAGS.model_dir))
    tf.gfile.DeleteRecursively(FLAGS.model_dir)
  tf.gfile.MakeDirs(FLAGS.model_dir)

  # Load training and testing data
  (x_train, y_train), (x_test, y_test) = cifar10.load_data()

  # Set training steps
  training_steps = int(round(FLAGS.epochs * (len(x_train) / FLAGS.batch_size)))
  with tf.Graph().as_default():
    (images, labels, handle,
     training_iterator, 
     heldout_iterator) = build_input_pipeline(x_train, x_test, y_train, y_test, 
                                        FLAGS.batch_size, 500)

    # Build the network
    if FLAGS.arch == "resnet":
        network = BayesResNet(IMAGE_SHAPE, 10,
                              vmean=FLAGS.prior_qmean, 
                              vconstraint=FLAGS.variance_threshold)
    elif FLAGS.arch == "vgg":
        network = BVGG(IMAGE_SHAPE, 10)

    # Determine the output based on network type
    model = network.build_model()
    logits = model(images)
    labels_distribution = tfd.Categorical(logits=logits)
    log_likelihood = labels_distribution.log_prob(labels)

    # Compute the -ELBO as the loss, averaged over the batch size.
    neg_log_likelihood = -tf.reduce_mean(log_likelihood)

    # Perform KL annealing
    t = tf.Variable(0.0)
    m = tf.divide(t, tf.Variable(FLAGS.kl_annealing * len(x_train) /FLAGS.batch_size))
    kl = sum(model.losses) / len(x_train) * tf.minimum(1.0, m)
    loss = neg_log_likelihood + kl

    # Build metrics for evaluation. Predictions are formed from a single forward
    # pass of the probabilistic layers. They are cheap but noisy predictions.
    predictions = tf.argmax(logits, axis=1)
    with tf.name_scope("train"):
        train_accuracy, train_accuracy_update_op = tf.metrics.accuracy(
                labels=labels, predictions=predictions)
    with tf.name_scope("valid"):
        valid_accuracy, valid_accuracy_update_op = tf.metrics.accuracy(
                labels=labels, predictions=predictions)

    with tf.name_scope("training"):
      opt = tf.train.AdamOptimizer(FLAGS.learning_rate)
      train_op = opt.minimize(loss)
      update_step_op = tf.assign(t, t + 1)

      sess = tf.Session()
      sess.run(tf.global_variables_initializer())
      sess.run(tf.local_variables_initializer())

      # Run the training loop
      train_handle = sess.run(training_iterator.string_handle())
      heldout_handle = sess.run(heldout_iterator.string_handle())
      for step in range(training_steps):
        _ = sess.run([train_op, train_accuracy_update_op, update_step_op],
                     feed_dict={handle: train_handle})

        # Manually print the frequency
        if step % 100 == 0:
          loss_value, accuracy_value, kl_value = sess.run(
              [loss, train_accuracy, kl], feed_dict={handle: train_handle})
          print("Step: {:>3d} Loss: {:.3f} Accuracy: {:.3f} KL: {:.3f}".format(
              step, loss_value, accuracy_value, kl_value))

        if (step+1) % FLAGS.eval_freq == 0:
          # Compute log prob of heldout set by averaging draws from the model:
          # p(heldout | train) = int_model p(heldout|model) p(model|train)
          #                   ~= 1/n * sum_{i=1}^n p(heldout | model_i)  
          # where model_i is a draw from the posterior p(model|train).
          probs = np.asarray([sess.run((labels_distribution.probs),
                                       feed_dict={handle: heldout_handle})
                              for _ in range(FLAGS.num_monte_carlo)])
          mean_probs = np.mean(probs, axis=0)

          image_vals, label_vals = sess.run((images, labels),
                                            feed_dict={handle: heldout_handle})
          heldout_lp = np.mean(np.log(mean_probs[np.arange(mean_probs.shape[0]),
                                                 label_vals.flatten()]))
          print( " ... Held-out nats: {:.3f}".format(heldout_lp) )

          # Calculate validation accuracy
          for _ in range(20):
              sess.run(valid_accuracy_update_op, feed_dict={handle: heldout_handle})
          valid_value = sess.run(valid_accuracy, feed_dict={handle: heldout_handle})

          print(" ... Validation Accuracy: {:.3f}".format(valid_value))

          # Reset validation variables
          stream_vars_valid = [v for v in tf.local_variables() if 'valid/' in v.name]
          sess.run(tf.variables_initializer(stream_vars_valid))
                                    
if __name__ == "__main__":
  tf.app.run()