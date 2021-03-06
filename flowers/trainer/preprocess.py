# Copyright 2016 Google Inc. All Rights Reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Example dataflow pipeline for preparing image training data.

The tool requires two main input files:

'input' - URI to csv file, using format:
gs://image_uri1,labela,labelb,labelc
gs://image_uri2,labela,labeld
...

'input_dict' - URI to a text file listing all labels (one label per line):
labela
labelb
labelc

The output data is in format accepted by Cloud ML framework.

This tool produces outputs as follows.
It creates one training example per each line of the created csv file.
When processing CSV file:
- all labels that are not present in input_dict are skipped

To execute this pipeline locally using default options, run this script
with no arguments. To execute on cloud pass single argument --cloud.

To execute this pipeline on the cloud using the Dataflow service and non-default
options:
python -E preprocess.py \
--input_path=PATH_TO_INPUT_CSV_FILE \
--input_dict=PATH_TO_INPUT_DIC_TXT_FILE \
--output_path=YOUR_OUTPUT_PATH \
--cloud

For other flags, see PrepareImagesOptions() bellow.

To run this pipeline locally run the above command without --cloud.

TODO(b/31434218)
"""

# TODO(mikehcheng): Beam convention for stage names is CapitalCase as opposed to
# English sentences (eg ReadAndConvertToJpeg as opposed to
# "Read and convert to JPEG"). Fix all samples that don't conform to the
# convention.

# TODO(mikehcheng): Standardize the casing of the various counters (metrics)
# used within this file. So far we have been using underscore_case for metrics.


import argparse
import csv
import datetime
import errno
import io
import logging
import os
import subprocess
import sys

import apache_beam as beam
from apache_beam.metrics import Metrics
from apache_beam.utils.pipeline_options import PipelineOptions
from PIL import Image
import tensorflow as tf

from tensorflow.contrib.slim.python.slim.nets import inception_v3 as inception
from tensorflow.python.framework import errors
from tensorflow.python.lib.io import file_io

slim = tf.contrib.slim

error_count = Metrics.counter('main', 'errorCount')
missing_label_count = Metrics.counter('main', 'missingLabelCount')
csv_rows_count = Metrics.counter('main', 'csvRowsCount')
labels_count = Metrics.counter('main', 'labelsCount')
labels_without_ids = Metrics.counter('main', 'labelsWithoutIds')
existing_file = Metrics.counter('main', 'existingFile')
non_existing_file = Metrics.counter('main', 'nonExistingFile')
skipped_empty_line = Metrics.counter('main', 'skippedEmptyLine')
embedding_good = Metrics.counter('main', 'embedding_good')
embedding_bad = Metrics.counter('main', 'embedding_bad')
incompatible_image = Metrics.counter('main', 'incompatible_image')
invalid_uri = Metrics.counter('main', 'invalid_file_name')
unlabeled_image = Metrics.counter('main', 'unlabeled_image')
unknown_label = Metrics.counter('main', 'unknown_label')

DISTORT_IMAGE_COUNT = 6

class Default(object):
  """Default values of variables."""
  FORMAT = 'jpeg'

  # Make sure to update the default checkpoint file if using another
  # inception graph or when a newer checkpoint file is available. See
  # https://research.googleblog.com/2016/08/improving-inception-and-image.html
  IMAGE_GRAPH_CHECKPOINT_URI = (
#       '/Users/mtv/data3/9gag/inception_v3_2016_08_28.ckpt')
      'gs://cloud-ml-data/img/flower_photos/inception_v3_2016_08_28.ckpt')


class ExtractLabelIdsDoFn(beam.DoFn):
  """Extracts (uri, label_ids) tuples from CSV rows.
  """

  def start_bundle(self, context=None):
    self.label_to_id_map = {}

  # The try except is for compatiblity across multiple versions of the sdk
  def process(self, row, all_labels):
    try:
      row = row.element
    except AttributeError:
      pass
    if not self.label_to_id_map:
      for i, label in enumerate(all_labels):
        label = label.strip()
        if label:
          self.label_to_id_map[label] = i

    # Row format is: image_uri(,label_ids)*
    if not row:
      skipped_empty_line.inc()
      return

    csv_rows_count.inc()
    uri = row[0]
    if not uri or not (uri.startswith('gs://') or uri.startswith('file://')):
      invalid_uri.inc()
      print("invalid uri " + str(uri))
      return

    # In a real-world system, you may want to provide a default id for labels
    # that were not in the dictionary.  In this sample, we simply skip it.
    # This code already supports multi-label problems if you want to use it.
    label_ids = []
    for label in row[1:]:
      try:
        label_ids.append(self.label_to_id_map[label.strip()])
      except KeyError:
        unknown_label.inc()

    labels_count.inc(len(label_ids))

    if not label_ids:
      unlabeled_image.inc()
    yield row[0], label_ids


class ReadImageAndConvertToJpegDoFn(beam.DoFn):
  """Read files from GCS and convert images to JPEG format.

  We do this even for JPEG images to remove variations such as different number
  of channels.
  """

  def process(self, element):
    try:
      uri, label_ids = element.element
    except AttributeError:
      uri, label_ids = element

    # TF will enable 'rb' in future versions, but until then, 'r' is
    # required.
    def _open_file_read_binary(uri):
      try:
        return file_io.FileIO(uri, mode='rb')
      except errors.InvalidArgumentError:
        print("error reading file %s" % uri)
        return file_io.FileIO(uri, mode='r')

    try:
      with _open_file_read_binary(uri) as f:
        image_bytes = f.read()
        img = Image.open(io.BytesIO(image_bytes)).convert('RGB')

    # A variety of different calling libraries throw different exceptions here.
    # They all correspond to an unreadable file so we treat them equivalently.
    except Exception as e:  # pylint: disable=broad-except
      print('Error processing image %s: %s' % (uri, str(e)))
      error_count.inc()
      return

    # Convert to desired format and output.
    output = io.BytesIO()
    img.save(output, Default.FORMAT)
    image_bytes = output.getvalue()
    yield uri, label_ids, image_bytes


class EmbeddingsGraph(object):
  """Builds a graph and uses it to extract embeddings from images.
  """

  # These constants are set by Inception v3's expectations.
  WIDTH = 299
  HEIGHT = 299
  CHANNELS = 3

  def __init__(self, tf_session):
    self.tf_session = tf_session
    # input_jpeg is the tensor that contains raw image bytes.
    # It is used to feed image bytes and obtain embeddings.
    self.input_jpeg, self.distorted_version, self.embedding = self.build_graph()

    init_op = tf.global_variables_initializer()
    self.tf_session.run(init_op)
    self.restore_from_checkpoint(Default.IMAGE_GRAPH_CHECKPOINT_URI)


  # distort_color is copied and possibly modified from
  # inception/inception/image_processing.py under
  # http://www.apache.org/licenses/LICENSE-2.0
  def distort_color(self, image, thread_id=0, scope=None):
    """Distort the color of the image.

    Each color distortion is non-commutative and thus ordering of the color ops
    matters. Ideally we would randomly permute the ordering of the color ops.
    Rather then adding that level of complication, we select a distinct ordering
    of color ops for each preprocessing thread.

    Args:
      image: Tensor containing single image.
      thread_id: preprocessing thread ID.
      scope: Optional scope for name_scope.
    Returns:
      color-distorted image
    """
    with tf.name_scope(values=[image], name=scope, default_name='distort_color'):
      color_ordering = thread_id % 2

      if color_ordering == 0:
        image = tf.image.random_brightness(image, max_delta=32. / 255.)
        image = tf.image.random_saturation(image, lower=0.5, upper=1.5)
        image = tf.image.random_hue(image, max_delta=0.2)
        image = tf.image.random_contrast(image, lower=0.5, upper=1.5)
      elif color_ordering == 1:
        image = tf.image.random_brightness(image, max_delta=32. / 255.)
        image = tf.image.random_contrast(image, lower=0.5, upper=1.5)
        image = tf.image.random_saturation(image, lower=0.5, upper=1.5)
        image = tf.image.random_hue(image, max_delta=0.2)

      # The random_* ops do not necessarily clamp.
      image = tf.clip_by_value(image, 0.0, 1.0)
      return image


  # distort_image is copied and possibly modified from
  # inception/inception/image_processing.py under
  # http://www.apache.org/licenses/LICENSE-2.0
  def distort_image(self, image, height, width, bbox, thread_id=0, scope=None):
    """Distort one image for training a network.

    Distorting images provides a useful technique for augmenting the data
    set during training in order to make the network invariant to aspects
    of the image that do not effect the label.

    Args:
      image: 3-D float Tensor of image
      height: integer
      width: integer
      bbox: 3-D float Tensor of bounding boxes arranged [1, num_boxes, coords]
        where each coordinate is [0, 1) and the coordinates are arranged
        as [ymin, xmin, ymax, xmax].
      thread_id: integer indicating the preprocessing thread.
      scope: Optional scope for name_scope.
    Returns:
      3-D float Tensor of distorted image used for training.
    """
    with tf.name_scope(values=[image, height, width, bbox], name=scope,
                       default_name='distort_image'):
      # Each bounding box has shape [1, num_boxes, box coords] and
      # the coordinates are ordered [ymin, xmin, ymax, xmax].

      # Display the bounding box in the first thread only.
      if not thread_id:
        image_with_box = tf.image.draw_bounding_boxes(tf.expand_dims(image, 0),
                                                      bbox)
        tf.summary.image('image_with_bounding_boxes', image_with_box)

      if thread_id:
        # A large fraction of image datasets contain a human-annotated bounding
        # box delineating the region of the image containing the object of interest.
        # We choose to create a new bounding box for the object which is a randomly
        # distorted version of the human-annotated bounding box that obeys an allowed
        # range of aspect ratios, sizes and overlap with the human-annotated
        # bounding box. If no box is supplied, then we assume the bounding box is
        # the entire image.
        sample_distorted_bounding_box = tf.image.sample_distorted_bounding_box(
          tf.shape(image),
          bounding_boxes=bbox,
          min_object_covered=0.1,
          aspect_ratio_range=[0.75, 1.33],
          #area_range=[0.05, 1.0],
          area_range=[0.50, 1.0],
          max_attempts=100,
          use_image_if_no_bounding_boxes=True)
        bbox_begin, bbox_size, distort_bbox = sample_distorted_bounding_box

        # Crop the image to the specified bounding box.
        distorted_image = tf.slice(image, bbox_begin, bbox_size)

      else: # not thread_id:
        distorted_image = image

      # This resizing operation may distort the images because the aspect
      # ratio is not respected. We select a resize method in a round robin
      # fashion based on the thread number.
      # Note that ResizeMethod contains 4 enumerated resizing methods.
      resize_method = thread_id % 4
      distorted_image = tf.image.resize_images(distorted_image, [height, width],
                                               method=resize_method)
      # Restore the shape since the dynamic slice based upon the bbox_size loses
      # the third dimension.
      distorted_image.set_shape([height, width, 3])

      if not thread_id:
        tf.summary.image('cropped_resized_image',
                         tf.expand_dims(distorted_image, 0))

        # Randomly flip the image horizontally.
        distorted_image = tf.image.random_flip_left_right(distorted_image)

        # Randomly distort the colors.
        distorted_image = self.distort_color(distorted_image, thread_id)

        tf.summary.image('final_distorted_image',
                         tf.expand_dims(distorted_image, 0))
      return distorted_image


  def case_arm(self, arm_value, input_value, image):
    arm = tf.equal(tf.constant(arm_value), input_value)
    bbox = tf.constant([[[0., 0., 1., 1.]]])
    image = self.distort_image(image, self.HEIGHT, self.WIDTH, bbox, arm_value)
    return arm, lambda: image
    
  def build_graph(self):
    """Forms the core by building a wrapper around the inception graph.

      Here we add the necessary input & output tensors, to decode jpegs,
      serialize embeddings, restore from checkpoint etc.

      To use other Inception models modify this file. Note that to use other
      models beside Inception, you should make sure input_shape matches
      their input. Resizing or other modifications may be necessary as well.
      See tensorflow/contrib/slim/python/slim/nets/inception_v3.py for
      details about InceptionV3.

    Returns:
      input_jpeg: A tensor containing raw image bytes as the input layer.
      embedding: The embeddings tensor, that will be materialized later.
    """

    distorted_version = tf.placeholder(tf.int32, name='distorted_version')
    input_jpeg = tf.placeholder(tf.string, shape=None)
    image = tf.image.decode_jpeg(input_jpeg, channels=self.CHANNELS)

    # convert_image_dtype also scales [0, uint8_max] -> [0 ,1).
    image = tf.image.convert_image_dtype(image, dtype=tf.float32)

    # distort based on distorted_version
    case_arms = {}
    for x in range(DISTORT_IMAGE_COUNT):
      guard, value = self.case_arm(x, distorted_version, image)
      case_arms[guard] = value
    image = tf.case(case_arms, default=lambda: image, exclusive=True)
    image.set_shape([self.HEIGHT, self.WIDTH, self.CHANNELS])


    # Then rescale range to [-1, 1) for Inception.
    image = tf.subtract(image, 0.5)
    inception_input = tf.multiply(image, 2.0)

    # Note resize expects a batch_size, but we are feeding a single image.
    # So we have to expand then squeeze.  Resize returns float32 in the
    # range [0, uint8_max]
    inception_input = tf.expand_dims(inception_input, 0)

    # Build Inception layers, which expect a tensor of type float from [-1, 1)
    # and shape [batch_size, height, width, channels].
    with slim.arg_scope(inception.inception_v3_arg_scope()):
      _, end_points = inception.inception_v3(inception_input, is_training=False)

    embedding = end_points['PreLogits']
    return input_jpeg, distorted_version, embedding

  def restore_from_checkpoint(self, checkpoint_path):
    """To restore inception model variables from the checkpoint file.

       Some variables might be missing in the checkpoint file, so it only
       loads the ones that are avialable, assuming the rest would be
       initialized later.
    Args:
      checkpoint_path: Path to the checkpoint file for the Inception graph.
    """
    # Get all variables to restore. Exclude Logits and AuxLogits because they
    # depend on the input data and we do not need to intialize them from
    # checkpoint.
    all_vars = tf.contrib.slim.get_variables_to_restore(
        exclude=['InceptionV3/AuxLogits', 'InceptionV3/Logits', 'global_step'])

    saver = tf.train.Saver(all_vars)
    saver.restore(self.tf_session, checkpoint_path)

  def calculate_embedding(self, batch_image_bytes, distorted_version=0):
    """Get the embeddings for a given JPEG image.

    Args:
      batch_image_bytes: As if returned from [ff.read() for ff in file_list].

    Returns:
      The Inception embeddings (bottleneck layer output)
    """
    return self.tf_session.run(
        self.embedding, feed_dict={self.input_jpeg: batch_image_bytes,
                                   self.distorted_version: distorted_version})


class TFExampleFromImageDoFn(beam.DoFn):
  """Embeds image bytes and labels, stores them in tensorflow.Example.

  (uri, label_ids, image_bytes) -> (tensorflow.Example).

  Output proto contains 'label', 'image_uri' and 'embedding'.
  The 'embedding' is calculated by feeding image into input layer of image
  neural network and reading output of the bottleneck layer of the network.

  Attributes:
    image_graph_uri: an uri to gcs bucket where serialized image graph is
                     stored.
  """

  def __init__(self, distort_images=False):
    self.tf_session = None
    self.graph = None
    self.preprocess_graph = None
    self.distort_image_count = DISTORT_IMAGE_COUNT if distort_images else 1

  def start_bundle(self, context=None):
    # There is one tensorflow session per instance of TFExampleFromImageDoFn.
    # The same instance of session is re-used between bundles.
    # Session is closed by the destructor of Session object, which is called
    # when instance of TFExampleFromImageDoFn() is destructed.
    if not self.graph:
      self.graph = tf.Graph()
      self.tf_session = tf.InteractiveSession(graph=self.graph)
      with self.graph.as_default():
        self.preprocess_graph = EmbeddingsGraph(self.tf_session)

  def process(self, element):

    def _bytes_feature(value):
      return tf.train.Feature(bytes_list=tf.train.BytesList(value=value))

    def _float_feature(value):
      return tf.train.Feature(float_list=tf.train.FloatList(value=value))

    try:
      element = element.element
    except AttributeError:
      pass
    uri, label_ids, image_bytes = element

    for version in range(self.distort_image_count):
      try:
        embedding = self.preprocess_graph.calculate_embedding(
          image_bytes, version)
      except errors.InvalidArgumentError as e:
        incompatible_image.inc()
        print('Could not encode an image from %s: %s' % (uri, str(e)))
        logging.warning('Could not encode an image from %s: %s', uri, str(e))
        return

      if embedding.any():
        embedding_good.inc()
      else:
        embedding_bad.inc()

      example = tf.train.Example(features=tf.train.Features(feature={
        'image_uri': _bytes_feature([uri]),
        'embedding': _float_feature(embedding.ravel().tolist()),
      }))

      if label_ids:
        label_ids.sort()
        example.features.feature['label'].int64_list.value.extend(label_ids)

      yield example


def configure_pipeline(p, opt):
  """Specify PCollection and transformations in pipeline."""
  read_input_source = beam.io.ReadFromText(
      opt.input_path, strip_trailing_newlines=True)
  read_label_source = beam.io.ReadFromText(
      opt.input_dict, strip_trailing_newlines=True)
  labels = (p | 'Read dictionary' >> read_label_source)
  _ = (p
       | 'Read input' >> read_input_source
       | 'Parse input' >> beam.Map(lambda line: csv.reader([line]).next())
       | 'Extract label ids' >> beam.ParDo(ExtractLabelIdsDoFn(),
                                           beam.pvalue.AsIter(labels))
       | 'Read and convert to JPEG'
       >> beam.ParDo(ReadImageAndConvertToJpegDoFn())
       | 'Embed and make TFExample'
       >> beam.ParDo(TFExampleFromImageDoFn(opt.distort_images))
       # TODO(b/35133536): Get rid of this Map and instead use
       # coder=beam.coders.ProtoCoder(tf.train.Example) in WriteToTFRecord
       # below.
       | 'SerializeToString' >> beam.Map(lambda x: x.SerializeToString())
       | 'Save to disk'
       >> beam.io.WriteToTFRecord(opt.output_path,
                                  file_name_suffix='.tfrecord.gz'))


def run(in_args=None):
  """Runs the pre-processing pipeline."""

  pipeline_options = PipelineOptions.from_dictionary(vars(in_args))
  with beam.Pipeline(options=pipeline_options) as p:
    configure_pipeline(p, in_args)


def default_args(argv):
  """Provides default values for Workflow flags."""
  parser = argparse.ArgumentParser()

  parser.add_argument(
      '--input_path',
      required=True,
      help='Input specified as uri to CSV file. Each line of csv file '
      'contains colon-separated GCS uri to an image and labels.')
  parser.add_argument(
      '--input_dict',
      dest='input_dict',
      required=True,
      help='Input dictionary. Specified as text file uri. '
      'Each line of the file stores one label.')
  parser.add_argument(
      '--output_path',
      required=True,
      help='Output directory to write results to.')
  parser.add_argument(
      '--project',
      type=str,
      help='The cloud project name to be used for running this pipeline')

  parser.add_argument(
      '--job_name',
      type=str,
      default='flowers-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S'),
      help='A unique job identifier.')
  parser.add_argument(
      '--num_workers', default=20, type=int, help='The number of workers.')
  parser.add_argument('--cloud', default=False, action='store_true')
  parser.add_argument(
      '--runner',
      help='See Dataflow runners, may be blocking'
      ' or not, on cloud or not, etc.')

  parsed_args, _ = parser.parse_known_args(argv)

  if parsed_args.cloud:
    # Flags which need to be set for cloud runs.
    default_values = {
        'project':
            get_cloud_project(),
        'temp_location':
            os.path.join(os.path.dirname(parsed_args.output_path), 'temp'),
        'runner':
            'DataflowRunner',
        'save_main_session':
            True,
    }
  else:
    # Flags which need to be set for local runs.
    default_values = {
        'runner': 'DirectRunner',
    }

  for kk, vv in default_values.iteritems():
    if kk not in parsed_args or not vars(parsed_args)[kk]:
      vars(parsed_args)[kk] = vv

  return parsed_args


def get_cloud_project():
  cmd = [
      'gcloud', '-q', 'config', 'list', 'project',
      '--format=value(core.project)'
  ]
  with open(os.devnull, 'w') as dev_null:
    try:
      res = subprocess.check_output(cmd, stderr=dev_null).strip()
      if not res:
        raise Exception('--cloud specified but no Google Cloud Platform '
                        'project found.\n'
                        'Please specify your project name with the --project '
                        'flag or set a default project: '
                        'gcloud config set project YOUR_PROJECT_NAME')
      return res
    except OSError as e:
      if e.errno == errno.ENOENT:
        raise Exception('gcloud is not installed. The Google Cloud SDK is '
                        'necessary to communicate with the Cloud ML service. '
                        'Please install and set up gcloud.')
      raise


def main(argv):
  arg_dict = default_args(argv)
  run(arg_dict)


if __name__ == '__main__':
  main(sys.argv[1:])
