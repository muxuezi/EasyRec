# -*- encoding:utf-8 -*-
# Copyright (c) Alibaba, Inc. and its affiliates.
# Date: 2018-09-13
"""Binary to run train and evaluation on recommendation model."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import json
import logging
import math
import os

import six
import tensorflow as tf
from tensorflow.core.protobuf import saved_model_pb2

import easy_rec
from easy_rec.python.builders import strategy_builder
from easy_rec.python.compat import exporter
from easy_rec.python.input.input import Input
from easy_rec.python.model.easy_rec_estimator import EasyRecEstimator
from easy_rec.python.model.easy_rec_model import EasyRecModel
from easy_rec.python.utils import config_util
from easy_rec.python.utils import estimator_utils
from easy_rec.python.utils import load_class
from easy_rec.python.utils.pai_util import is_on_pai

if tf.__version__ >= '2.0':
  gfile = tf.compat.v1.gfile
  from tensorflow.core.protobuf import config_pb2

  ConfigProto = config_pb2.ConfigProto
  GPUOptions = config_pb2.GPUOptions
else:
  gfile = tf.gfile
  GPUOptions = tf.GPUOptions
  ConfigProto = tf.ConfigProto

load_class.auto_import()

# when version of tensorflow > 1.8 strip_default_attrs set true will cause
# saved_model inference core, such as:
#   [libprotobuf FATAL external/protobuf_archive/src/google/protobuf/map.h:1058]
#    CHECK failed: it != end(): key not found: new_axis_mask
# so temporarily modify strip_default_attrs of _SavedModelExporter in
# tf.estimator.exporter to false by default
FinalExporter = exporter.FinalExporter
LatestExporter = exporter.LatestExporter
BestExporter = exporter.BestExporter


def _get_input_fn(data_config,
                  feature_configs,
                  data_path=None,
                  export_config=None):
  """Build estimator input function.

  Args:
    data_config:  dataset config
    feature_configs: FeatureConfig
    data_path: input_data_path
    export_config: configuration for exporting models,
      only used to build input_fn when exporting models

  Returns:
    subclass of Input
  """
  input_class_map = {
      data_config.CSVInput: 'CSVInput',
      data_config.CSVInputV2: 'CSVInputV2',
      data_config.OdpsInput: 'OdpsInput',
      data_config.OdpsInputV2: 'OdpsInputV2',
      data_config.RTPInput: 'RTPInput',
      data_config.RTPInputV2: 'RTPInputV2',
      data_config.OdpsRTPInput: 'OdpsRTPInput',
      data_config.DummyInput: 'DummyInput',
      data_config.KafkaInput: 'KafkaInput'
  }

  input_cls_name = input_class_map[data_config.input_type]
  input_class = Input.create_class(input_cls_name)

  task_id, task_num = estimator_utils.get_task_index_and_num()
  input_obj = input_class(
      data_config,
      feature_configs,
      data_path,
      task_index=task_id,
      task_num=task_num)
  input_fn = input_obj.create_input(export_config)
  return input_fn


def _create_estimator(pipeline_config, distribution=None, params={}):
  model_config = pipeline_config.model_config
  train_config = pipeline_config.train_config
  gpu_options = GPUOptions(allow_growth=False)
  session_config = ConfigProto(
      gpu_options=gpu_options,
      allow_soft_placement=True,
      log_device_placement=False)
  session_config.device_filters.append('/job:ps')
  model_cls = EasyRecModel.create_class(model_config.model_class)

  save_checkpoints_steps = None
  save_checkpoints_secs = None
  if train_config.HasField('save_checkpoints_steps'):
    save_checkpoints_steps = train_config.save_checkpoints_steps
  if train_config.HasField('save_checkpoints_secs'):
    save_checkpoints_secs = train_config.save_checkpoints_secs
  # if both `save_checkpoints_steps` and `save_checkpoints_secs` are not set,
  # use the default value of save_checkpoints_steps
  if save_checkpoints_steps is None and save_checkpoints_secs is None:
    save_checkpoints_steps = train_config.save_checkpoints_steps

  run_config = tf.estimator.RunConfig(
      model_dir=pipeline_config.model_dir,
      log_step_count_steps=train_config.log_step_count_steps,
      save_summary_steps=train_config.save_summary_steps,
      save_checkpoints_steps=save_checkpoints_steps,
      save_checkpoints_secs=save_checkpoints_secs,
      train_distribute=distribution,
      eval_distribute=distribution,
      session_config=session_config)

  estimator = EasyRecEstimator(
      pipeline_config, model_cls, run_config=run_config, params=params)
  return estimator, run_config


def _create_eval_export_spec(pipeline_config, eval_data):
  data_config = pipeline_config.data_config
  feature_configs = pipeline_config.feature_configs
  eval_config = pipeline_config.eval_config
  export_config = pipeline_config.export_config
  if eval_config.num_examples > 0:
    eval_steps = int(
        math.ceil(float(eval_config.num_examples) / data_config.batch_size))
    logging.info('eval_steps = %d' % eval_steps)
  else:
    eval_steps = None
  # create eval input
  export_input_fn = _get_input_fn(data_config, feature_configs, None,
                                  export_config)
  if export_config.exporter_type == 'final':
    exporters = [
        FinalExporter(name='final', serving_input_receiver_fn=export_input_fn)
    ]
  elif export_config.exporter_type == 'latest':
    exporters = [
        LatestExporter(
            name='latest',
            serving_input_receiver_fn=export_input_fn,
            exports_to_keep=1)
    ]
  elif export_config.exporter_type == 'best':
    logging.info(
        'will use BestExporter, metric is %s, the bigger the better: %d' %
        (export_config.best_exporter_metric, export_config.metric_bigger))

    def _metric_cmp_fn(best_eval_result, current_eval_result):
      logging.info('metric: best = %s current = %s' %
                   (str(best_eval_result), str(current_eval_result)))
      if export_config.metric_bigger:
        return (best_eval_result[export_config.best_exporter_metric] <
                current_eval_result[export_config.best_exporter_metric])
      else:
        return (best_eval_result[export_config.best_exporter_metric] >
                current_eval_result[export_config.best_exporter_metric])

    exporters = [
        BestExporter(
            name='best',
            serving_input_receiver_fn=export_input_fn,
            compare_fn=_metric_cmp_fn)
    ]
  elif export_config.exporter_type == 'none':
    exporters = []
  else:
    raise ValueError('Unknown exporter type %s' % export_config.exporter_type)

  # set throttle_secs to a small number, so that we can control evaluation
  # interval steps by checkpoint saving steps
  eval_input_fn = _get_input_fn(data_config, feature_configs, eval_data)
  eval_spec = tf.estimator.EvalSpec(
      name='val',
      input_fn=eval_input_fn,
      steps=eval_steps,
      throttle_secs=10,
      exporters=exporters)
  return eval_spec


def _check_model_dir(model_dir, continue_train):
  if not continue_train:
    if not gfile.IsDirectory(model_dir):
      gfile.MakeDirs(model_dir)
    else:
      assert len(gfile.Glob(model_dir + '/model.ckpt-*.meta')) == 0, \
          'model_dir[=%s] already exists and not empty(if you ' \
          'want to continue train on current model_dir please ' \
          'delete dir %s or specify --continue_train[internal use only])' % (
              model_dir, model_dir)
  else:
    if not gfile.IsDirectory(model_dir):
      logging.info('%s does not exists, create it automatically' % model_dir)
      gfile.MakeDirs(model_dir)


def _get_ckpt_path(pipeline_config, checkpoint_path):
  if checkpoint_path != '' and checkpoint_path is not None:
    ckpt_path = checkpoint_path
  elif gfile.IsDirectory(pipeline_config.model_dir):
    ckpt_path = tf.train.latest_checkpoint(pipeline_config.model_dir)
    logging.info('checkpoint_path is not specified, '
                 'will use latest checkpoint %s from %s' %
                 (ckpt_path, pipeline_config.model_dir))
  else:
    assert False, 'pipeline_config.model_dir(%s) does not exist' \
                  % pipeline_config.model_dir
  return ckpt_path


def train_and_evaluate(pipeline_config_path, continue_train=False):
  """Train and evaluate a EasyRec model defined in pipeline_config_path.

  Build an EasyRecEstimator, and then train and evaluate the estimator.

  Args:
    pipeline_config_path: a path to EasyRecConfig object, specifies
    train_config: model_config, data_config and eval_config
    continue_train: whether to restart train from an existing
                    checkpoint
  Returns:
    None, the model will be saved into pipeline_config.model_dir
  """
  assert gfile.Exists(pipeline_config_path), 'pipeline_config_path not exists'
  pipeline_config = config_util.get_configs_from_pipeline_file(
      pipeline_config_path)

  _train_and_evaluate_impl(pipeline_config, continue_train)

  return pipeline_config


def _train_and_evaluate_impl(pipeline_config, continue_train=False):
  # Tempoary for EMR
  if (not is_on_pai()) and 'TF_CONFIG' in os.environ:
    tf_config = json.loads(os.environ['TF_CONFIG'])
    # for ps on emr currently evaluator is not supported
    # the cluster has one chief instead of master
    # evaluation will not be done, so we replace chief with master
    if 'cluster' in tf_config and 'chief' in tf_config[
        'cluster'] and 'ps' in tf_config['cluster'] and (
            'evaluator' not in tf_config['cluster']):
      chief = tf_config['cluster']['chief']
      del tf_config['cluster']['chief']
      tf_config['cluster']['master'] = chief
      if tf_config['task']['type'] == 'chief':
        tf_config['task']['type'] = 'master'
      os.environ['TF_CONFIG'] = json.dumps(tf_config)

  train_config = pipeline_config.train_config
  data_config = pipeline_config.data_config
  feature_configs = pipeline_config.feature_configs

  if pipeline_config.WhichOneof('train_path') == 'kafka_train_input':
    train_data = pipeline_config.kafka_train_input
  else:
    train_data = pipeline_config.train_input_path

  if pipeline_config.WhichOneof('eval_path') == 'kafka_eval_input':
    eval_data = pipeline_config.kafka_eval_input
  else:
    eval_data = pipeline_config.eval_input_path

  export_config = pipeline_config.export_config
  if export_config.dump_embedding_shape:
    embed_shape_dir = os.path.join(pipeline_config.model_dir,
                                   'embedding_shapes')
    if not gfile.Exists(embed_shape_dir):
      gfile.MakeDirs(embed_shape_dir)
    easy_rec._global_config['dump_embedding_shape_dir'] = embed_shape_dir
    pipeline_config.train_config.separate_save = True

  distribution = strategy_builder.build(train_config)
  estimator, run_config = _create_estimator(
      pipeline_config, distribution=distribution)

  master_stat_file = os.path.join(pipeline_config.model_dir, 'master.stat')
  version_file = os.path.join(pipeline_config.model_dir, 'version')
  if run_config.is_chief:
    _check_model_dir(pipeline_config.model_dir, continue_train)
    config_util.save_pipeline_config(pipeline_config, pipeline_config.model_dir)
    with gfile.GFile(version_file, 'w') as f:
      f.write(easy_rec.__version__ + '\n')
    if gfile.Exists(master_stat_file):
      gfile.Remove(master_stat_file)

  train_steps = pipeline_config.train_config.num_steps
  if train_steps <= 0:
    train_steps = None
    logging.warn('will train INFINITE number of steps')
  else:
    logging.info('train_steps = %d' % train_steps)
  # create train input
  train_input_fn = _get_input_fn(data_config, feature_configs, train_data)
  # Currently only a single Eval Spec is allowed.
  train_spec = tf.estimator.TrainSpec(
      input_fn=train_input_fn, max_steps=train_steps)
  # create eval spec
  eval_spec = _create_eval_export_spec(pipeline_config, eval_data)
  tf.estimator.train_and_evaluate(estimator, train_spec, eval_spec)
  logging.info('Train and evaluate finish')


def evaluate(pipeline_config,
             eval_checkpoint_path='',
             eval_data_path=None,
             eval_result_filename='eval_result.txt'):
  """Evaluate a EasyRec model defined in pipeline_config_path.

  Evaluate the model defined in pipeline_config_path on the eval data,
  the metrics will be displayed on tensorboard and saved into eval_result.txt.

  Args:
    pipeline_config: either EasyRecConfig path or its instance
    eval_checkpoint_path: if specified, will use this model instead of
        model specified by model_dir in pipeline_config_path
    eval_data_path: eval data path, default use eval data in pipeline_config
        could be a path or a list of paths
    eval_result_filename: evaluation result metrics save path.

  Returns:
    A dict of evaluation metrics: the metrics are specified in
        pipeline_config_path
    global_step: the global step for which this evaluation was performed.

  Raises:
    AssertionError, if:
      * pipeline_config_path does not exist
  """
  pipeline_config = config_util.get_configs_from_pipeline_file(pipeline_config)
  if eval_data_path is not None:
    logging.info('Evaluating on data: %s' % eval_data_path)
    if isinstance(eval_data_path, list):
      pipeline_config.eval_data.input_path[:] = eval_data_path
    else:
      pipeline_config.eval_data.input_path[:] = [eval_data_path]
  train_config = pipeline_config.train_config

  if pipeline_config.WhichOneof('eval_path') == 'kafka_eval_input':
    eval_data = pipeline_config.kafka_eval_input
  else:
    eval_data = pipeline_config.eval_input_path

  distribution = strategy_builder.build(train_config)
  estimator, _ = _create_estimator(pipeline_config, distribution)
  eval_spec = _create_eval_export_spec(pipeline_config, eval_data)

  ckpt_path = _get_ckpt_path(pipeline_config, eval_checkpoint_path)

  eval_result = estimator.evaluate(
      eval_spec.input_fn, eval_spec.steps, checkpoint_path=ckpt_path)
  logging.info('Evaluate finish')

  # write eval result to file
  model_dir = pipeline_config.model_dir
  eval_result_file = os.path.join(model_dir, eval_result_filename)
  logging.info('save eval result to file %s' % eval_result_file)
  with gfile.GFile(eval_result_file, 'w') as ofile:
    result_to_write = {}
    for key in sorted(eval_result):
      # skip logging binary data
      if isinstance(eval_result[key], six.binary_type):
        continue
      # convert numpy float to python float
      result_to_write[key] = eval_result[key].item()

    ofile.write(json.dumps(result_to_write, indent=2))

  return eval_result


def predict(pipeline_config, checkpoint_path='', data_path=None):
  """Predict a EasyRec model defined in pipeline_config_path.

  Predict the model defined in pipeline_config_path on the eval data.

  Args:
    pipeline_config: either EasyRecConfig path or its instance
    checkpoint_path: if specified, will use this model instead of
        model specified by model_dir in pipeline_config_path
    data_path: data path, default use eval data in pipeline_config
        could be a path or a list of paths

  Returns:
    A list of dict of predict results

  Raises:
    AssertionError, if:
      * pipeline_config_path does not exist
  """
  pipeline_config = config_util.get_configs_from_pipeline_file(pipeline_config)
  if data_path is not None:
    logging.info('Predict on data: %s' % data_path)
    pipeline_config.eval_input_path = data_path
  train_config = pipeline_config.train_config
  if pipeline_config.WhichOneof('eval_path') == 'kafka_eval_input':
    eval_data = pipeline_config.kafka_eval_input
  else:
    eval_data = pipeline_config.eval_input_path

  distribution = strategy_builder.build(train_config)
  estimator, _ = _create_estimator(pipeline_config, distribution)
  eval_spec = _create_eval_export_spec(pipeline_config, eval_data)

  ckpt_path = _get_ckpt_path(pipeline_config, checkpoint_path)

  pred_result = estimator.predict(eval_spec.input_fn, checkpoint_path=ckpt_path)
  logging.info('Predict finish')
  return pred_result


def export(export_dir, pipeline_config_path, checkpoint_path=''):
  """Export model defined in pipeline_config_path.

  Args:
    export_dir: base directory where the model should be exported
    pipeline_config_path: file specify proto.EasyRecConfig, including
       model_config, eval_data, eval_config
    checkpoint_path: if specified, will use this model instead of
       model in model_dir in pipeline_config_path

  Returns:
    the directory where model is exported

  Raises:
    AssertionError, if:
      * pipeline_config_path does not exist
  """
  assert gfile.Exists(pipeline_config_path), 'pipeline_config_path is empty'
  if not gfile.Exists(export_dir):
    gfile.MakeDirs(export_dir)

  pipeline_config = config_util.get_configs_from_pipeline_file(
      pipeline_config_path)
  feature_configs = pipeline_config.feature_configs

  estimator, _ = _create_estimator(pipeline_config)
  # construct serving input fn
  export_config = pipeline_config.export_config
  data_config = pipeline_config.data_config
  serving_input_fn = _get_input_fn(data_config, feature_configs, None,
                                   export_config)

  # pack embedding.pb into asset_extras
  assets_extra = None
  if export_config.dump_embedding_shape:
    embed_shape_dir = os.path.join(pipeline_config.model_dir,
                                   'embedding_shapes')
    easy_rec._global_config['dump_embedding_shape_dir'] = embed_shape_dir
    # determine model version
    if checkpoint_path == '':
      tmp_ckpt_path = tf.train.latest_checkpoint(pipeline_config.model_dir)
    else:
      tmp_ckpt_path = checkpoint_path
    ckpt_ver = tmp_ckpt_path.split('-')[-1]

    embed_files = gfile.Glob(
        os.path.join(pipeline_config.model_dir, 'embeddings',
                     '*.pb.' + ckpt_ver))
    assets_extra = {}
    for one_file in embed_files:
      _, one_file_name = os.path.split(one_file)
      assets_extra[one_file_name] = one_file

  if checkpoint_path != '':
    final_export_dir = estimator.export_savedmodel(
        export_dir_base=export_dir,
        serving_input_receiver_fn=serving_input_fn,
        checkpoint_path=checkpoint_path,
        assets_extra=assets_extra,
        strip_default_attrs=True)
  else:
    final_export_dir = estimator.export_savedmodel(
        export_dir_base=export_dir,
        serving_input_receiver_fn=serving_input_fn,
        assets_extra=assets_extra,
        strip_default_attrs=True)

  # add export ts as version info
  saved_model = saved_model_pb2.SavedModel()
  if type(final_export_dir) not in [type(''), type(u'')]:
    final_export_dir = final_export_dir.decode('utf-8')
  export_ts = [
      x for x in final_export_dir.split('/') if x != '' and x is not None
  ]
  export_ts = export_ts[-1]
  saved_pb_path = os.path.join(final_export_dir, 'saved_model.pb')
  with gfile.GFile(saved_pb_path, 'rb') as fin:
    saved_model.ParseFromString(fin.read())
  saved_model.meta_graphs[0].meta_info_def.meta_graph_version = export_ts
  with gfile.GFile(saved_pb_path, 'wb') as fout:
    fout.write(saved_model.SerializeToString())

  logging.info('model has been exported to %s successfully' % final_export_dir)
  return final_export_dir
