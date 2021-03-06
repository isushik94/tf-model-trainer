import itertools
import math
import os
import shutil
import tensorflow as tf
import time
import types
import operator
from .device_utils import get_available_gpus, local_device_setter
from enum import Enum

if not hasattr(tf.data, 'experimental'):
    tfdata = tf.contrib.data
else:
    tfdata = tf.data.experimental


class DatasetIteratorNames:
    Training = 'train'
    Validation = 'valid'


class Dataset:
    class _DatasetType(Enum):
        Manual = -1
        TensorSlices = 0
        Generator = 1

    def __init__(self, **kwargs):
        '''
        kwargs:
            'batch_size' : specifies a batch size.
            'buffer_size_factor' : specifies a buffer size factor which is multiplied on a batch size for shuffle op.
            'enable_caching' : allows dataset's tensors caching (default: False).
            'cache_dir_path' : the path to a directory where the cache will be placed (default: None).
            'n_workers' : the number of workers used in dataset's map functions (default: the number of cores).
            'prefetch_size': the number of batches to be prefetched.
        '''
        self._params = kwargs.copy()
        self._datasets = []
        self._compiled = False
        self._iterators = dict()

    def add_source(self, *args, **kwargs):
        '''Adds a dataset for training.

        1. `add_dataset(placeholders_getter, feed_dict_getter [, mapper=None[, needs_flatting=False]])`

        2. `add_dataset(dataset)`

        Parameters
        ---------
        placeholders_getter
            a function which provides a tuple of placeholders to form a `tf.data.Dataset` instance.

        feed_dict_getter
            a function which provides a dictionary to be feed with the placeholders as a keys.
            this function must have the following signature:

                def feed_dict_getter(state, *placeholders): pass

        mapper : optional
            a function which is used in `tf.data.Dataset.map`

        needs_flatting : bool, optional
            specifies whether to flat `mapper` results with `tf.data.Dataset.flat_map`

        dataset : object
            an instance of a class which implements one of the following interfaces:

                class Dataset:
                    needs_flatting = bool()

                    def placeholders(self): pass
                    def map_func(self): pass # optional
                    def feed_dict(self, state): pass

            or

                class Dataset:
                    needs_flatting = bool()

                    def generator(self): pass
                    def map_func(self): pass # optional
                    def feed_dict(self, state): pass

            or

                class Dataset:
                    def get_dataset(self): pass
                    def feed_dict(self, state): pass

        Notes
        -----
        The `state` parameter can have only the following values: { 'train', 'valid' }

        '''
        if self._compiled:
            raise RuntimeError('Dataset is already compiled')

        if len(args) == 0:
            raise ValueError('No inputs provided.')

        weight = max(0., kwargs.get('weight', 1.))

        if len(args) > 1:
            placeholders_getter = args[0]
            feed_dict_getter = args[1]
            dataset_mapper = args[2] if len(args) > 2 else None
            needs_flatting = args[3] if len(args) > 3 else False

            if not callable(placeholders_getter):
                raise ValueError('placeholders_getter: is not callable')

            if not callable(feed_dict_getter):
                raise ValueError('feed_dict_getter: is not callable')

            if dataset_mapper is not None and not callable(dataset_mapper):
                raise ValueError('dataset_mapper: is not callable')

            class AnonymousDataset:
                def __init__(self):
                    self.needs_flatting = needs_flatting
                    self._placeholders = None

                    if dataset_mapper is not None:
                        self.map_func = self._map_func

                def placeholders(self):
                    if self._placeholders is None:
                        self._placeholders = placeholders_getter()
                    return self._placeholders

                def _map_func(self, *args):
                    if dataset_mapper is not None:
                        return dataset_mapper(*args)

                def feed_dict(self, state):
                    return feed_dict_getter(state, *self._placeholders)

            self._datasets.append((AnonymousDataset(), self._DatasetType.TensorSlices, weight))
        else:
            dataset = args[0]

            if isinstance(dataset, type):
                dataset = dataset()

            if hasattr(dataset, 'placeholders'):
                if not hasattr(dataset, 'feed_dict'):
                    raise ValueError('dataset: has not `feed_dict` method')

                self._datasets.append((dataset, self._DatasetType.TensorSlices, weight))
            elif hasattr(dataset, 'generator'):
                self._datasets.append((dataset, self._DatasetType.Generator, weight))
            elif hasattr(dataset, 'get_dataset'):
                self._datasets.append((dataset, self._DatasetType.Manual, weight))
            else:
                raise ValueError('dataset: has neither `placeholders` nor `get_dataset` methods')

        return self

    def compile(self, graph=None, scope=None, **kwargs):
        if self._compiled:
            raise RuntimeError('Dataset is already compiled')

        if graph is None:
            graph = tf.get_default_graph()

        if kwargs:
            params = self._params.copy()
            params.update(kwargs)
        else:
            params = self._params

        with graph.as_default():
            with tf.name_scope(scope, 'dataset'):
                self._setup_dataset(params)
                self._compiled = True

    def _setup_dataset(self, params):
        if not len(self._datasets):
            raise ValueError('No inputs souces were secified.')

        batch_size = params.get('batch_size', 1)
        buffer_size_factor = params.get('buffer_size_factor', 1)
        dataset_enable_caching = params.get('enable_caching', False)
        dataset_cache_dir_path = params.get('cache_dir_path', None)
        dataset_n_workers = params.get('n_workers', os.cpu_count())
        prefetch_size = max(1, params.get('prefetch_size', 1))

        padded_batch = params.get('padded_batch', None)
        pad_shapes = params.get('pad_shapes', None)
        pad_values = params.get('pad_values', None)
        pad_drop_remainder = params.get('pad_drop_remainder', None)

        if dataset_cache_dir_path and not dataset_cache_dir_path.endswith('/'):
            dataset_cache_dir_path = dataset_cache_dir_path + '/'

        self._dataset_iterator_name = tf.placeholder(tf.string, name='iterator_name')

        datasets = []
        for i, (dataset_provider, dtype, weight) in enumerate(self._datasets):
            dataset_n_workers_this = max(1, dataset_n_workers // len(self._datasets))
            if dtype == self._DatasetType.Manual:
                dataset = dataset_provider.get_dataset()
            else:
                if dtype == self._DatasetType.TensorSlices:
                    dataset_placeholders = dataset_provider.placeholders()
                    dataset = tf.data.Dataset.from_tensor_slices(dataset_placeholders)
                elif dtype == self._DatasetType.Generator:
                    dataset_generator_args = dataset_provider.generator()
                    if not isinstance(dataset_generator_args, tuple):
                        raise ValueError('Dataset `%s`: not return a tuple from `generator` method' % type(dataset_provider))

                    if len(dataset_generator_args) < 2:
                        raise ValueError('Dataset `%s`: a generator tuple must contain at least a generator method and a list of output types' % type(dataset_provider))

                    args = [self._dataset_iterator_name]
                    if len(dataset_generator_args) > 3:
                        args = args + list(dataset_generator_args[3])
                        dataset_generator_args = dataset_generator_args[:3]

                    dataset = tf.data.Dataset.from_generator(*dataset_generator_args, args=tuple(args))

                if hasattr(dataset_provider, 'map_func') and dataset_provider.map_func is not None:
                    dataset = dataset.prefetch(dataset_n_workers_this + 1)
                    dataset = dataset.map(dataset_provider.map_func, dataset_n_workers_this)

                    if hasattr(dataset_provider, 'ignore_errors') and dataset_provider.ignore_errors:
                        dataset = dataset.apply(tfdata.ignore_errors())

                if dataset_enable_caching:
                    if dataset_cache_dir_path is not None:
                        if not os.path.exists(dataset_cache_dir_path):
                            os.makedirs(dataset_cache_dir_path)
                        dataset = dataset.cache(tf.constant(dataset_cache_dir_path + ('data-%i-' % i)) + self._dataset_iterator_name)
                    else:
                        dataset = dataset.cache()

                needs_flatting = hasattr(dataset_provider, 'needs_flatting') and dataset_provider.needs_flatting
                if needs_flatting:
                    dataset = dataset.apply(tfdata.parallel_interleave(
                        lambda *samples: tf.data.Dataset.from_tensor_slices(samples), cycle_length=1))

                if any([s.ndims is None for s in dataset.output_shapes]):
                    tf.logging.warning('The dataset (%s) has unknown shapes %s.' % (type(dataset), dataset.output_shapes))

                if len(datasets) > 0:
                    assert datasets[-1][0].output_types == dataset.output_types,\
                        'Datasets don\'t produce the same types of elements'
                    assert datasets[-1][0].output_shapes == dataset.output_shapes,\
                        'Datasets don\'t produce the same shapes of elements'

            dataset = dataset.repeat()

            datasets.append((dataset, weight))

        if len(datasets) == 1:
            dataset, _ = datasets[0]
        else:
            def concatenate(datasets):
                if len(datasets) == 0:
                    return None
                else:
                    d = tf.data.Dataset.from_tensors(datasets[0])
                    o = concatenate(datasets[1:])
                    if o is not None:
                        d = d.concatenate(o)
                    return d

            datasets, weights = zip(*datasets)

            weights_sum = sum(weights)
            weights = [int(round((w / weights_sum) / 0.05)) for w in weights]
            n_total = sum(weights)

            datasets_weighted = []
            for dataset, repeats in zip(datasets, weights):
                if repeats > 0:
                    datasets_weighted = datasets_weighted + [dataset.batch(repeats).prefetch(1)]

            dataset = tf.data.Dataset.zip(tuple(datasets_weighted))
            dataset = dataset.apply(tfdata.parallel_interleave(lambda *ds: concatenate(ds), cycle_length=1))
            dataset = dataset.apply(tfdata.parallel_interleave(lambda *samples: tf.data.Dataset.from_tensor_slices(samples), cycle_length=1))
            dataset = dataset.shuffle(buffer_size=max(0, int(n_total*buffer_size_factor)))

        dataset = dataset.shuffle(buffer_size=max(0, int(buffer_size_factor*batch_size)))

        if callable(padded_batch):
            padded_batch = padded_batch()

        if not padded_batch:
            dataset = dataset.batch(batch_size=batch_size)
        else:
            if callable(pad_shapes):
                pad_shapes = pad_shapes()

            pad_shapes = tuple([((u if isinstance(u, tf.TensorShape) else tf.TensorShape(u)) if u is not None else s)
                                for u, s in zip(pad_shapes, dataset.output_shapes)])

            if callable(pad_values):
                pad_values = pad_values()

            if isinstance(pad_values, list):
                pad_values = tuple(pad_values)

            if callable(pad_drop_remainder):
                pad_drop_remainder = pad_drop_remainder()

            if any([s.ndims is None for s in pad_shapes]):
                tf.logging.warning('Padded shapes has unspecified values %s, batch selection will be without padding.' % pad_shapes)
                dataset = dataset.batch(batch_size=batch_size)
            elif isinstance(pad_values, tuple) and len(pad_values) != len(pad_shapes):
                tf.logging.warning('Padding values aren\'t specified for all shapes (pad_shapes(%i) != pad_values(%i)), batch selection will be without padding.' % (len(pad_shapes), len(pad_values)))
                dataset = dataset.batch(batch_size=batch_size)
            else:
                if isinstance(pad_values, tuple):
                    pad_values = tuple([tf.cast(v, dtype=t) for v, t in zip(pad_values, dataset.output_types)])

                dataset = dataset.padded_batch(batch_size=batch_size, padded_shapes=pad_shapes, padding_values=pad_values, drop_remainder=pad_drop_remainder)

        dataset = dataset.prefetch(buffer_size=prefetch_size)

        self.dataset = dataset

        self._train_iterator = dataset.make_initializable_iterator('train')
        self._valid_iterator = dataset.make_initializable_iterator('valid')

        self._train_batch = self._train_iterator.get_next
        self._valid_batch = self._valid_iterator.get_next

    def outputs(self, pipe, name=None):
        if pipe not in [DatasetIteratorNames.Training, DatasetIteratorNames.Validation]:
            raise ValueError('Unsupported pipe: %r' % pipe)

        if pipe not in self._iterators:
            if isinstance(pipe, str):
                name = pipe

            iterator = self.dataset.make_initializable_iterator(name)
            self._iterators[pipe] = (iterator, name)

        iterator = self._iterators[pipe][0]
        return iterator


    def init(self, session):
        for pipe, (iterator, _) in self._iterators.items():
            feed_dict = dict()
            for dataset, _, _ in self._datasets:
                if not hasattr(dataset, 'feed_dict'):
                    continue

                feed = dataset.feed_dict(pipe)
                if feed:
                    feed_dict.update(feed)

            for k, v in feed_dict.items():
                try:
                    if len(v) == 0 and not isinstance(v, (bytes, str)):
                        tf.logging.warning('Possible empty a training data source: `%r` = %r' % (k, v))
                except:
                    pass

            feed_dict[self._dataset_iterator_name] = pipe

            session.run(iterator.initializer, feed_dict)


class Trainer:
    class _DatasetType(Enum):
        Manual = -1
        TensorSlices = 0
        Generator = 1

    def __init__(self, hparams=None, **kwargs):
        """Initializes a Trainer instance.

        Parameters:
        ---------
        hparams : :obj:`tf.contrib.training.HParams`, optional
            Parameters used by the Trainer to construct a computational graph and to execute training loop.
            The following params are in use:
                'training_dir_path' : specifies a directory which is used for storing checkpoints and summaries (default: './training').
                'n_training_steps' : the number of training steps (iterations) (default: 10000).
                'n_checkpoint_steps' : the number of steps (iterations) per checkpoint saving (default: 1000).
                'n_summary_steps' : the number of steps (iterations) per summary writing (default: 1000).
                'allow_restoring' : allows restoring from a saved checkpoint (default: True).
                'gpu_memory_fraction': sets the fraction of total gpu's memory to be used (default: 0.95).
                'place_vars_on_cpu' : whether to place variables on CPU or not (default: False).
                'batch_size' : specifies a batch size.
                'buffer_size' : specifies a buffer size.
                'dataset_enable_caching' : allows dataset's tensors caching (default: False).
                'dataset_cache_dir_path' : the path to a directory where the cache will be placed (default: None).
                'dataset_n_workers' : the number of workers used in dataset's map functions (default: the number of cores).
                'multigpu_sync_steps' : the number of iterations on a GPU before synchonization of gradients (default: 1).
                'use_gready_placement_startegy' : forces to use `tf.contrib.training.GreedyLoadBalancingStrategy` for variable placing (default: False).
                'grad_clip_value' : sets gradients clipping value for `tf.clip_by_value` (default: None).
                'grad_clip_norm' : sets gradients clipping value for `tf.clip_by_norm` (default: None).
                'learning_rate' : the value of a learning rate for an optimizer (default: 0.1).
                'learning_rate_decay' : if it's values is set, it enables learning rate decay (default: None).
                'learning_rate_n_decay_steps' : the number of steps to apply decay factor (default: 1000)
                'learning_rate_decay_staircase' : whether learning rate decaying should look like stairs (default: False).
                'optimizer' : sets an optimizer (Adam|SGD|Momentum)
                'optimizer_params' : a dict of parameters to be passed to an instance of optimizer
                'use_nccl': a flag

        **kwargs
            Arbitrary keyword arguments. These arguments override hparams.
        """
        self.hparams = tf.contrib.training.HParams()

        if hparams is not None:
            for key, value in hparams.values().items():
                if key in self.hparams:
                    self.hparams.set_hparam(key, value)
                else:
                    self.hparams.add_hparam(key, value)

        for key, value in kwargs.items():
            if key in self.hparams:
                self.hparams.set_hparam(key, value)
            else:
                self.hparams.add_hparam(key, value)

        self._is_builded = False
        self.saver = None

        self._learning_rate_getter = None
        self._optimizer_getter = None

        self._datasets = []
        self._freeze_suffix = None

    def add_dataset(self, *args, **kwargs):
        '''Adds a dataset for training.

        1. `add_dataset(placeholders_getter, feed_dict_getter [, mapper=None[, needs_flatting=False]])`

        2. `add_dataset(dataset)`

        Parameters
        ---------
        placeholders_getter
            a function which provides a tuple of placeholders to form a `tf.data.Dataset` instance.

        feed_dict_getter
            a function which provides a dictionary to be feed with the placeholders as a keys.
            this function must have the following signature:

                def feed_dict_getter(state, *placeholders): pass

        mapper : optional
            a function which is used in `tf.data.Dataset.map`

        needs_flatting : bool, optional
            specifies whether to flat `mapper` results with `tf.data.Dataset.flat_map`

        dataset : object
            an instance of a class which implements one of the following interfaces:

                class Dataset:
                    needs_flatting = bool()

                    def placeholders(self): pass
                    def map_func(self): pass # optional
                    def feed_dict(self, state): pass

            or

                class Dataset:
                    needs_flatting = bool()

                    def generator(self): pass
                    def map_func(self): pass # optional
                    def feed_dict(self, state): pass

            or

                class Dataset:
                    def get_dataset(self): pass
                    def feed_dict(self, state): pass

        Notes
        -----
        The `state` parameter can have only the following values: { 'train', 'valid' }

        '''
        if len(args) == 0:
            raise ValueError('No inputs provided.')

        weight = max(0., kwargs.get('weight', 1.))

        if len(args) > 1:
            placeholders_getter = args[0]
            feed_dict_getter = args[1]
            dataset_mapper = args[2] if len(args) > 2 else None
            needs_flatting = args[3] if len(args) > 3 else False

            if not callable(placeholders_getter):
                raise ValueError('placeholders_getter: is not callable')

            if not callable(feed_dict_getter):
                raise ValueError('feed_dict_getter: is not callable')

            if dataset_mapper is not None and not callable(dataset_mapper):
                raise ValueError('dataset_mapper: is not callable')

            class AnonymousDataset:
                def __init__(self):
                    self.needs_flatting = needs_flatting
                    self._placeholders = None

                    if dataset_mapper is not None:
                        self.map_func = self._map_func

                def placeholders(self):
                    if self._placeholders is None:
                        self._placeholders = placeholders_getter()
                    return self._placeholders

                def _map_func(self, *args):
                    if dataset_mapper is not None:
                        return dataset_mapper(*args)

                def feed_dict(self, state):
                    return feed_dict_getter(state, *self._placeholders)

            self._datasets.append((AnonymousDataset(), self._DatasetType.TensorSlices, weight))
        else:
            dataset = args[0]

            if isinstance(dataset, type):
                dataset = dataset()

            if hasattr(dataset, 'placeholders'):
                if not hasattr(dataset, 'feed_dict'):
                    raise ValueError('dataset: has not `feed_dict` method')

                self._datasets.append((dataset, self._DatasetType.TensorSlices, weight))
            elif hasattr(dataset, 'generator'):
                self._datasets.append((dataset, self._DatasetType.Generator, weight))
            elif hasattr(dataset, 'get_dataset'):
                self._datasets.append((dataset, self._DatasetType.Manual, weight))
            else:
                raise ValueError('dataset: has neither `placeholders` nor `get_dataset` methods')

        self._is_builded = False

        return self

    def set_model(self, model_getter, var_scope='model'):
        '''
        Parameters
        ---------
        model_getter : type
            An instance of some model type. The type must have `forward`, `loss`, `gradients` methods:

                class Model:
                    def forward(self, *inputs): pass
                    def loss(self, scope): pass
                    def gradients(self): pass

        var_scope : str, optional
            Forces to create a variable scope with a provided name
        Returns
        -------
        Trainer
            the instance
        '''
        if not hasattr(model_getter, 'forward'):
            raise ValueError('model_getter: has not `forward` method')

        if not hasattr(model_getter, 'loss'):
            raise ValueError('model_getter: has not `loss` method')

        if not hasattr(model_getter, 'gradients'):
            raise ValueError('model_getter: has not `gradients` method')

        if isinstance(model_getter, type):
            self._model_getter = model_getter
        else:
            self._model_getter = lambda: model_getter

        self._var_scope = var_scope
        self._is_builded = False

        return self

    def set_learning_rate_op(self, learning_rate_getter):
        if not callable(learning_rate_getter):
            raise ValueError('learning_rate_getter: is not callable')

        self._learning_rate_getter = learning_rate_getter

    def set_custom_optimizer(self, optimizer_getter):
        if not callable(optimizer_getter):
            raise ValueError('optimizer_getter: is not callable')

        self._optimizer_getter = optimizer_getter

    def set_metrics(self, metrics_getter):
        '''
        Arguments
        ---------
        metrics_getter
        '''
        self.metrics_getter = metrics_getter

        self._is_builded = False

        return self

    def set_summary(self, summary_getter):
        '''
        Arguments
        ---------
        summary_getter
        '''
        self.summary_getter = summary_getter

        self._is_builded = False

        return self

    def train(self, verbose=False, training_dir_path=None, profile=False, add_numeric_check=False, config_params=None):
        self._build_graph()
        
        if training_dir_path is None:
            training_dir_path = self.hparams.get('training_dir_path', './training')

        n_training_steps = self.hparams.get('n_training_steps', 10000)
        n_checkpoint_steps = self.hparams.get('n_checkpoint_steps', 1000)
        n_summary_steps = self.hparams.get('n_summary_steps', 1000)

        allow_restoring = self.hparams.get('allow_restoring', True)
        gpu_memory_fraction = self.hparams.get('gpu_memory_fraction', 0.95)

        if not os.path.exists(training_dir_path):
            os.makedirs(training_dir_path)

        checkpoint_path = os.path.join(training_dir_path, 'model.ckpt')

        gpu_options = tf.GPUOptions(allow_growth=True,
                                    per_process_gpu_memory_fraction=gpu_memory_fraction)
        
        if not isinstance(config_params, dict):
            config_params = dict()
        
        if 'allow_soft_placement' not in config_params:
            config_params['allow_soft_placement'] = True
            
        if 'gpu_options' not in config_params:
            config_params['gpu_options'] = gpu_options
            
        config = tf.ConfigProto(**config_params)

        if profile:
            run_metadata = tf.RunMetadata()

        if add_numeric_check:
            check_op = tf.add_check_numerics_ops()
        else:
            check_op = None

        with tf.Session(config=config) as sess:
            try:
                if verbose:
                    print('Initializing parameters ', flush=True, end='')

                sess.run(self._init_globals_op)
                sess.run(self._init_locals_op)

                train_iter_feed_dict = dict()
                for dataset, _, _ in self._datasets:
                    if not hasattr(dataset, 'feed_dict'):
                        continue

                    feed = dataset.feed_dict(DatasetIteratorNames.Training)
                    if feed:
                        train_iter_feed_dict.update(feed)

                for k, v in train_iter_feed_dict.items():
                    try:
                        if len(v) == 0 and not isinstance(v, (bytes, str)):
                            tf.logging.warning('Possible empty a training data source: `%r` = %r' % (k, v))
                    except:
                        pass

                train_iter_feed_dict[self.dataset_iterator_name] = DatasetIteratorNames.Training

                sess.run(self.train_iterator.initializer, train_iter_feed_dict)

                valid_iter_feed_dict = dict()
                for dataset, _, _ in self._datasets:
                    if not hasattr(dataset, 'feed_dict'):
                        continue

                    feed = dataset.feed_dict(DatasetIteratorNames.Validation)
                    if feed:
                        valid_iter_feed_dict.update(feed)

                for k, v in valid_iter_feed_dict.items():
                    try:
                        if len(v) == 0 and not isinstance(v, (bytes, str)):
                            tf.logging.warning('Possible empty a validation data source: `%r` = %r' % (k, v))
                    except:
                        pass

                valid_iter_feed_dict[self.dataset_iterator_name] = DatasetIteratorNames.Validation

                sess.run(self.valid_iterator.initializer, valid_iter_feed_dict)

                if verbose:
                    print('[OK]', flush=True)
            except:
                if verbose:
                    print('[Failed]', flush=True)

                    import traceback
                    traceback.print_exc()

                raise

            model = self._towers_models[0]
            if hasattr(model, 'preload_weights_op') and callable(model.preload_weights_op):
                model.preload_weights_op()(sess)

            ckpt = tf.train.get_checkpoint_state(training_dir_path)
            if allow_restoring and ckpt and tf.train.checkpoint_exists(ckpt.model_checkpoint_path):
                try:
                    self.saver.restore(sess, ckpt.model_checkpoint_path)
                except:
                    self.param_saver.restore(sess, ckpt.model_checkpoint_path)
            else:
                shutil.rmtree(training_dir_path)
                self.saver.save(sess, checkpoint_path)

            tf.train.write_graph(sess.graph_def, training_dir_path, 'graph.pb', as_text=False)

            _train_summary_writer = tf.summary.FileWriter(os.path.join(training_dir_path, 'summary', 'train'), sess.graph)
            _valid_summary_writer = tf.summary.FileWriter(os.path.join(training_dir_path, 'summary', 'valid'), sess.graph)

            try:
                _step = int(sess.run(self._step_var))
                if _step == 0:
                    _train_loss, _train_summary = sess.run([self.total_loss, self.train_summary_op], {self.data_loader_mode: 'train-pipe'})
                    _valid_loss, _valid_summary = sess.run([self.total_loss, self.valid_summary_op], {self.data_loader_mode: 'valid-pipe'})
                    _train_summary_writer.add_summary(_train_summary, _step)
                    _valid_summary_writer.add_summary(_valid_summary, _step)
                    _train_summary_writer.flush()
                    _valid_summary_writer.flush()

                    if verbose:
                        print('Initial train loss = %.6f, valid loss = %.6f.' % (_train_loss, _valid_loss), flush=True)

                if verbose:
                    print('Start trainging.', flush=True)

                if verbose:
                    start = time.time()

                train_ops = [self.train_op]
                if check_op is not None:
                    train_ops.append(check_op)

                for _ in range(_step, n_training_steps):
                    if profile:
                        sess.run(train_ops, {self.is_training_mode: True, self.data_loader_mode: 'train-pipe'}, options=tf.RunOptions(trace_level=tf.RunOptions.FULL_TRACE), run_metadata=run_metadata)
                    else:
                        sess.run(train_ops, {self.is_training_mode: True, self.data_loader_mode: 'train-pipe'})

                    _step = int(sess.run(self._step_var))

                    if _step % n_summary_steps == 0:
                        _train_loss, _train_summary = sess.run([self.total_loss, self.train_summary_op], {self.data_loader_mode: 'train-pipe'})
                        _valid_loss, _valid_summary = sess.run([self.total_loss, self.valid_summary_op], {self.data_loader_mode: 'valid-pipe'})
                        _train_summary_writer.add_summary(_train_summary, _step)
                        if profile:
                            _train_summary_writer.add_run_metadata(run_metadata, 'train-op-%i' % _step, _step)
                        _valid_summary_writer.add_summary(_valid_summary, _step)
                        _train_summary_writer.flush()
                        _valid_summary_writer.flush()

                        if verbose:
                            elapsed = time.time() - start
                            start = time.time()
                            print('Step #%i: train loss = %.6f, valid loss = %.6f, elapsed %.3f sec.' % (_step, _train_loss, _valid_loss, elapsed), flush=True)

                    if _step % n_checkpoint_steps == 0:
                        try:
                            if verbose:
                                print('Saving checkpoint...',  flush=True, end='')

                            self.saver.save(sess, checkpoint_path, global_step=_step)

                            if verbose:
                                print('[OK]', flush=True)
                        except:
                            if verbose:
                                print('[Failed]', flush=True)
                            raise

                if verbose:
                    print('Training process is finished.', flush=True)
            except:
                if verbose:
                    import traceback
                    traceback.print_exc()
                raise
            finally:
                self.saver.save(sess, checkpoint_path, global_step=_step)
                tf.train.write_graph(sess.graph_def, training_dir_path, 'graph.pb', as_text=False)
                if profile:
                    tf.profiler.advise(sess.graph, run_metadata)

    def freeze(self, input_getter, outputs_names=None,
               training_dir_path=None, ckpt_path=None, graph_protected_nodes=None,
               model_scope='model', frozen_name='graph.frozen', verbose=False):
        """
        """
        session = None

        if not input_getter:
            raise ValueError('input_getter: is empty')

        if not callable(input_getter):
            raise ValueError('input_getter: is not callable')

        if training_dir_path is None:
            training_dir_path = self.hparams.get('training_dir_path', './training')

        ckpt = tf.train.get_checkpoint_state(training_dir_path)

        if ckpt_path is None:
            ckpt_path = ckpt.model_checkpoint_path if ckpt else None

        if not ckpt_path or not tf.train.checkpoint_exists(ckpt.model_checkpoint_path):
            raise ValueError('Model is not trained.')

        graph = tf.Graph()
        with graph.as_default():
            var_scope = tf.get_variable_scope() if self._var_scope is None else self._var_scope
            name_scope = graph.get_name_scope() if not model_scope else model_scope
            with tf.variable_scope(var_scope, auxiliary_name_scope=False):
                with tf.name_scope(name_scope):
                    inputs = input_getter()
                    if not isinstance(inputs, (tuple, list)):
                        inputs = [inputs]

                    model = self._model_getter()
                    if hasattr(model, 'inference') and callable(model.inference):
                        outputs = model.inference(*inputs)
                    else:
                        outputs = model.forward(False, *inputs)

                if not isinstance(outputs, (tuple, list)):
                    outputs = [outputs]

                def flat_names(tensors):
                    result = []
                    for t in tensors:
                        if isinstance(t, (tuple, list)):
                            result = result + flat_names(t)
                        else:
                            result.append(t.name)
                    return result

                if verbose:
                    print('The model\'s inputs:')
                    for name in flat_names(inputs):
                        print(' ', name)

                    print('The model\'s outputs:')
                    for name in flat_names(outputs):
                        print(' ', name)

                if not outputs_names:
                    outputs_names = flat_names(outputs)

                tf.graph_util.remove_training_nodes(graph.as_graph_def(), graph_protected_nodes)

                #model_saver = tf.train.Saver(tf.global_variables())

        def freeze_op(sess):
            sess.run(tf.global_variables_initializer())

            outputs = [sess.graph.get_tensor_by_name(item if item.rfind(':') != -1 else item + ':0') for item in outputs_names]

            previous_variables = [var_name for var_name, _ in tf.contrib.framework.list_variables(ckpt.model_checkpoint_path)]

            for variable in tf.global_variables():
                if variable.op.name in previous_variables:
                    var = tf.contrib.framework.load_variable(ckpt.model_checkpoint_path, variable.op.name)
                    tf.add_to_collection('assignOps', variable.assign(tf.cast(var, variable.dtype)))

            sess.run(tf.get_collection('assignOps'))

            # model_saver.restore(sess, ckpt.model_checkpoint_path)

            output_graph_def = tf.graph_util.convert_variables_to_constants(
                sess,
                sess.graph.as_graph_def(),
                [node.name.split(':')[0] for node in outputs]
            )

            if self._freeze_suffix is None:
                frozen_fname = '%s.pb' % frozen_name
            else:
                frozen_fname = '%s-%s.pb' % (frozen_name, str(self._freeze_suffix))

            with tf.gfile.GFile(os.path.join(training_dir_path, frozen_fname), "wb") as f:
                f.write(output_graph_def.SerializeToString())

            if verbose:
                flops = tf.profiler.profile(sess.graph, options=tf.profiler.ProfileOptionBuilder.float_operation())
                print('%d ops in the final graph.' % len(output_graph_def.node))
                print('%d flops in the final graph.' % flops.total_float_ops)
                print('The frozen graph is stored in file: `%s`' % os.path.join(training_dir_path, frozen_fname))

        if session is not None:
            pass
        else:
            config = tf.ConfigProto(allow_soft_placement=True, gpu_options=tf.GPUOptions(allow_growth=True))
            with tf.Session(config=config, graph=graph) as sess:
                freeze_op(sess)

    #
    # private section
    #

    def _build_graph(self):
        if not self._is_builded:
            tf.reset_default_graph()

            self._gpus = get_available_gpus()

            def build():
                with tf.name_scope('dataset'):
                    self._setup_dataset()

                with tf.name_scope('model') as scope:
                    with tf.name_scope('placeholders'):
                        self.is_training_mode = tf.placeholder_with_default(False, [], name='is_training_mode')
                        self.data_loader_mode = tf.placeholder_with_default('train-pipe', [], name='data_loader_mode')

                    self._setup_model(scope)

                with tf.name_scope('training'):
                    self._setup_train_op()

                    self.saver = tf.train.Saver(tf.global_variables())
                    self.param_saver = tf.train.Saver(list(tf.trainable_variables()) + [self._step_var_i])

                    self._setup_metrics()

                self._setup_summary()

                self._init_globals_op = tf.global_variables_initializer()
                self._init_locals_op = tf.local_variables_initializer()

                self._is_builded = True

            if self.hparams.get('place_vars_on_cpu', False):
                with tf.device('/cpu:0'):
                    return build()
            elif self._gpus:
                with tf.device(self._gpus[0]):
                    return build()
            else:
                return build()

    def _setup_dataset(self):
        if not len(self._datasets):
            raise ValueError('No inputs souces were secified.')

        N_TRAINERS = max(1, len(self._gpus))

        batch_size = self.hparams.get('batch_size', 1)
        buffer_size_factor = self.hparams.get('buffer_size_factor', 1)
        dataset_enable_caching = self.hparams.get('dataset_enable_caching', False)
        dataset_cache_dir_path = self.hparams.get('dataset_cache_dir_path', None)
        dataset_n_workers = self.hparams.get('dataset_n_workers', os.cpu_count())
        multigpu_sync_steps = max(1, self.hparams.get('multigpu_sync_steps', 1))

        if dataset_cache_dir_path and not dataset_cache_dir_path.endswith('/'):
            dataset_cache_dir_path = dataset_cache_dir_path + '/'

        self.dataset_iterator_name = tf.placeholder(tf.string, name='iterator_name')

        datasets = []
        for i, (dataset_provider, dtype, weight) in enumerate(self._datasets):
            dataset_n_workers_this = max(1, dataset_n_workers // len(self._datasets))
            if dtype == self._DatasetType.Manual:
                dataset = dataset_provider.get_dataset()
            else:
                if dtype == self._DatasetType.TensorSlices:
                    dataset_placeholders = dataset_provider.placeholders()
                    dataset = tf.data.Dataset.from_tensor_slices(dataset_placeholders)
                elif dtype == self._DatasetType.Generator:
                    dataset_generator_args = dataset_provider.generator()
                    if not isinstance(dataset_generator_args, tuple):
                        raise ValueError('Dataset `%s`: not return a tuple from `generator` method' % type(dataset_provider))

                    if len(dataset_generator_args) < 2:
                        raise ValueError('Dataset `%s`: a generator tuple must contain at least a generator method and a list of output types' % type(dataset_provider))

                    args = [self.dataset_iterator_name]
                    if len(dataset_generator_args) > 3:
                        args = args + list(dataset_generator_args[3])
                        dataset_generator_args = dataset_generator_args[:3]

                    dataset = tf.data.Dataset.from_generator(*dataset_generator_args, args=tuple(args))

                if hasattr(dataset_provider, 'map_func') and dataset_provider.map_func is not None:
                    dataset = dataset.prefetch(dataset_n_workers_this + 1)
                    dataset = dataset.map(dataset_provider.map_func, dataset_n_workers_this)

                    if hasattr(dataset_provider, 'ignore_errors') and dataset_provider.ignore_errors:
                        dataset = dataset.apply(tfdata.ignore_errors())

                if dataset_enable_caching:
                    if dataset_cache_dir_path is not None:
                        if not os.path.exists(dataset_cache_dir_path):
                            os.makedirs(dataset_cache_dir_path)
                        dataset = dataset.cache(tf.constant(dataset_cache_dir_path + ('data-%i-' % i)) + self.dataset_iterator_name)
                    else:
                        dataset = dataset.cache()

                needs_flatting = hasattr(dataset_provider, 'needs_flatting') and dataset_provider.needs_flatting
                if needs_flatting:
                    dataset = dataset.apply(tfdata.parallel_interleave(
                        lambda *samples: tf.data.Dataset.from_tensor_slices(samples), cycle_length=1))

                if any([s.ndims is None for s in dataset.output_shapes]):
                    tf.logging.warning('The dataset (%s) has unknown shapes %s.' % (type(dataset), dataset.output_shapes))

                if len(datasets) > 0:
                    assert datasets[-1][0].output_types == dataset.output_types,\
                        'Datasets don\'t produce the same types of elements'
                    assert datasets[-1][0].output_shapes == dataset.output_shapes,\
                        'Datasets don\'t produce the same shapes of elements'

            dataset = dataset.repeat()

            datasets.append((dataset, weight))

        if len(datasets) == 1:
            dataset, _ = datasets[0]
        else:
            def concatenate(datasets):
                if len(datasets) == 0:
                    return None
                else:
                    d = tf.data.Dataset.from_tensors(datasets[0])
                    o = concatenate(datasets[1:])
                    if o is not None:
                        d = d.concatenate(o)
                    return d

            datasets, weights = zip(*datasets)

            weights_sum = sum(weights)
            weights = [int(round((w / weights_sum) / 0.05)) for w in weights]
            n_total = sum(weights)

            datasets_weighted = []
            for dataset, repeats in zip(datasets, weights):
                if repeats > 0:
                    datasets_weighted = datasets_weighted + [dataset.batch(repeats).prefetch(1)]

            dataset = tf.data.Dataset.zip(tuple(datasets_weighted))
            dataset = dataset.apply(tfdata.parallel_interleave(lambda *ds: concatenate(ds), cycle_length=1))
            dataset = dataset.apply(tfdata.parallel_interleave(lambda *samples: tf.data.Dataset.from_tensor_slices(samples), cycle_length=1))
            dataset = dataset.shuffle(buffer_size=max(0, int(n_total*buffer_size_factor)))

        dataset = dataset.shuffle(buffer_size=max(0, int(buffer_size_factor*batch_size)))

        padded_batch = hasattr(self._model_getter, 'padded_batch') and self._model_getter.padded_batch
        if callable(padded_batch):
            padded_batch = padded_batch()

        if not padded_batch:
            dataset = dataset.batch(batch_size=batch_size)
        else:
            pad_shapes = None
            if hasattr(self._model_getter, 'pad_shapes'):
                pad_shapes = self._model_getter.pad_shapes
                if callable(pad_shapes):
                    pad_shapes = pad_shapes()

            pad_shapes = tuple([((u if isinstance(u, tf.TensorShape) else tf.TensorShape(u)) if u is not None else s)
                                for u, s in zip(pad_shapes, dataset.output_shapes)])

            pad_values = None
            if hasattr(self._model_getter, 'pad_values'):
                pad_values = self._model_getter.pad_values
                if callable(pad_values):
                    pad_values = pad_values()

                if isinstance(pad_values, list):
                    pad_values = tuple(pad_values)

            pad_drop_remainder = hasattr(self._model_getter, 'pad_drop_remainder') and self._model_getter.pad_drop_remainder
            if callable(pad_drop_remainder):
                pad_drop_remainder = pad_drop_remainder()

            if any([s.ndims is None for s in pad_shapes]):
                tf.logging.warning('Padded shapes has unspecified values %s, batch selection will be without padding.' % pad_shapes)
                dataset = dataset.batch(batch_size=batch_size)
            elif isinstance(pad_values, tuple) and len(pad_values) != len(pad_shapes):
                tf.logging.warning('Padding values aren\'t specified for all shapes (pad_shapes(%i) != pad_values(%i)), batch selection will be without padding.' % (len(pad_shapes), len(pad_values)))
                dataset = dataset.batch(batch_size=batch_size)
            else:
                if isinstance(pad_values, tuple):
                    pad_values = tuple([tf.cast(v, dtype=t) for v, t in zip(pad_values, dataset.output_types)])

                dataset = dataset.padded_batch(batch_size=batch_size, padded_shapes=pad_shapes, padding_values=pad_values, drop_remainder=pad_drop_remainder)

        dataset = dataset.prefetch(buffer_size=(N_TRAINERS*(multigpu_sync_steps if N_TRAINERS > 1 else 1) + 1))

        self.dataset = dataset

        self.train_iterator = dataset.make_initializable_iterator('train')
        self.valid_iterator = dataset.make_initializable_iterator('valid')

        self.train_batch = self.train_iterator.get_next
        self.valid_batch = self.valid_iterator.get_next

    def _get_device_setter(self, name):
        if self.hparams.get('place_vars_on_cpu', False):
            return local_device_setter(worker_device=name)
        else:
            return local_device_setter(
                ps_device_type='gpu',
                worker_device=name,
                ps_strategy=(None if not self.hparams.get('use_gready_placement_startegy', False)
                             else tf.contrib.training.GreedyLoadBalancingStrategy(
                                    len(self._gpus), tf.contrib.training.byte_size_load_fn)))

    def _setup_model(self, parent_scope):
        self._towers_models = []
        self._towers_grads = []

        use_nccl = self.hparams.get('use_nccl', False)
        multigpu_sync_steps = max(1, self.hparams.get('multigpu_sync_steps', 1))

        def build_model(scope=None, grads_factor=1.):
            _batch = tf.case([(tf.equal(self.data_loader_mode, 'train-pipe'), lambda: self.train_batch()),
                              (tf.equal(self.data_loader_mode, 'valid-pipe'), lambda: self.valid_batch())],
                             exclusive=True)

            _batch = [tf.identity(item, name='batch/item-%i' % i) for i, item in enumerate(_batch)]

            model = self._model_getter()

            def scoped(scope):
                model.forward(self.is_training_mode, *_batch)

                losses = model.loss(scope)

                if not losses:
                    losses = tf.losses.get_losses(scope=scope)

                gradvars = model.gradients()

                if len(self._gpus) > 1:
                    with tf.name_scope('grads-division-for-avg'):
                        multiplier = tf.constant(grads_factor / len(self._gpus), dtype=tf.float32)
                        gradvars = [((tf.multiply(grad, tf.cast(multiplier, grad.dtype)) if grad is not None else grad), var) for grad, var in gradvars]

                return model, losses, gradvars

            if scope:
                with scope as scope:
                    return scoped(scope)
            else:
                return scoped(None)

        var_scope = tf.get_variable_scope() if self._var_scope is None else self._var_scope
        if len(self._gpus) > 1:
            for i, name in enumerate(self._gpus):
                with tf.device(self._get_device_setter(name)):
                    result_set = []
                    for s in range(multigpu_sync_steps):
                        with tf.variable_scope(var_scope, reuse=tf.AUTO_REUSE, auxiliary_name_scope=False):
                            if multigpu_sync_steps == 1:
                                scope = tf.name_scope('tower-%i' % i)
                            else:
                                scope = tf.name_scope('tower-%i-%i' % (i, s))

                            result = build_model(scope, grads_factor=1./multigpu_sync_steps)

                            result_set.append(result)

                    if len(result_set) == 1:
                        model, losses_, gradvars = result_set[0]
                    else:
                        models, losses_list, gradvars = zip(*result_set)

                        model = models[0]
                        losses_ = losses_list[0]

                        with tf.name_scope('gradient-summing'):
                            all_grads = {}
                            for grad, var in itertools.chain(*gradvars):
                                if grad is not None:
                                    all_grads.setdefault(var, []).append(grad)

                            gradvars = []
                            for var, grads in all_grads.items():
                                if len(grads) == 1:
                                    avg_grad = grads[0]
                                else:
                                    avg_grad = tf.add_n(grads)

                                with tf.device(var.device):
                                    if use_nccl:
                                        avg_grad = tf.contrib.nccl.broadcast(avg_grad)
                                    else:
                                        avg_grad = tf.identity(avg_grad)

                                gradvars.append((avg_grad, var))

                    self._towers_models.append(model)
                    self._towers_grads.append(gradvars)

                    if i == 0:
                        self._update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
                        losses = losses_
        else:
            with tf.variable_scope(var_scope, auxiliary_name_scope=False):
                if len(self._gpus):
                    with tf.device(self._get_device_setter(self._gpus[0])):
                        model, losses, gradvars = build_model()
                else:
                    model, losses, gradvars = build_model()

                self._towers_models.append(model)
                self._towers_grads.append(gradvars)

                self._update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)

        self.total_loss = tf.add_n(losses)

    def _setup_train_op(self):
        use_nccl = self.hparams.get('use_nccl', False)
        multigpu_sync_steps = max(1, self.hparams.get('multigpu_sync_steps', 1))

        grad_clip_value = self.hparams.get('grad_clip_value', None)

        if grad_clip_value is not None:
            if isinstance(grad_clip_value, (list, tuple)):
                grad_clip_value_min, grad_clip_value_max = grad_clip_value
            else:
                grad_clip_value_min = -float(grad_clip_value)
                grad_clip_value_max = float(grad_clip_value)

        grad_clip_norm = self.hparams.get('grad_clip_norm', None)
        if grad_clip_norm is not None:
            grad_clip_norm = float(grad_clip_norm)

        step_var = tf.Variable(0, trainable=False)

        with tf.name_scope('optimizer'):
            with tf.name_scope('params'):
                if self._learning_rate_getter is not None:
                    lr_var = self._learning_rate_getter(step_var)
                else:
                    learning_rate = self.hparams.get('learning_rate', 0.01)
                    learning_rate_decay = self.hparams.get('learning_rate_decay', None)
                    learning_rate_n_decay_steps = self.hparams.get('learning_rate_n_decay_steps', 1000)
                    learning_rate_decay_staircase = self.hparams.get('learning_rate_decay_staircase', False)

                    lr_var = tf.Variable(learning_rate, trainable=False)

                    if learning_rate_decay and learning_rate_n_decay_steps:
                        lr_var = tf.train.exponential_decay(
                            lr_var, step_var, learning_rate_n_decay_steps, learning_rate_decay,
                            staircase=learning_rate_decay_staircase
                        )

            if len(self._gpus) > 1:
                n_steps = multigpu_sync_steps
            else:
                n_steps = 1

            self._learning_rate = lr_var * n_steps

            if self._optimizer_getter is not None:
                _optimizer = self._optimizer_getter(self._learning_rate)
            else:
                opt_params = self.hparams.get('optimizer_params', dict())
                opt_code = str(self.hparams.get('optimizer', 'adam')).lower()
                if opt_code in ['adam']:
                    _optimizer = tf.train.AdamOptimizer(lr_var, **opt_params)
                elif opt_code in ['moment', 'momentum']:
                    _optimizer = tf.train.MomentumOptimizer(lr_var, **opt_params)
                elif opt_code in ['sgd', 'gd', 'gradiendescent', 'stochasticgradiendescent']:
                    _optimizer = tf.train.GradientDescentOptimizer(lr_var, **opt_params)
                else:
                    raise ValueError('Unsupported optimizer was set')

            def norm_var_name(name):
                return name[:-2].replace('/', '-')

            with tf.name_scope('gradients'):
                all_grads = {}
                for grad, var in itertools.chain(*self._towers_grads):
                    if grad is not None:
                        all_grads.setdefault(var, []).append(grad)

                gradvars = []
                for var, grads in all_grads.items():
                    with tf.name_scope('grad-' + norm_var_name(var.name)):
                        with tf.device(var.device):
                            if len(grads) == 1:
                                avg_grad = tf.identity(grads[0])
                            else:
                                if use_nccl:
                                    avg_grad = tf.contrib.nccl.reduce_sum(grads)
                                else:
                                    avg_grad = tf.add_n(grads)

                            if grad_clip_value is not None:
                                with tf.name_scope('clip-by-value'):
                                    tf_grad_clip_value_min = tf.constant(grad_clip_value_min, dtype=avg_grad.dtype)
                                    tf_grad_clip_value_max = tf.constant(grad_clip_value_max, dtype=avg_grad.dtype)
                                    avg_grad = tf.clip_by_value(avg_grad, tf_grad_clip_value_min, tf_grad_clip_value_max)

                            if grad_clip_norm is not None:
                                with tf.name_scope('clip-by-norm'):
                                    tf_grad_clip_norm = tf.constant(grad_clip_norm, dtype=avg_grad.dtype)
                                    avg_grad = tf.clip_by_norm(avg_grad, tf_grad_clip_norm)

                    gradvars.append((avg_grad, var))

            apply_gradient_op = _optimizer.apply_gradients(gradvars, global_step=step_var)

            self._grads = gradvars
            self.train_op = apply_gradient_op
            self._step_var = step_var * n_steps
            self._step_var_i = step_var

            if self._update_ops:
                self.train_op = tf.group(self.train_op, *self._update_ops)

    def _setup_metrics(self):
        with tf.name_scope('metrics'):
            model = self._towers_models[0]
            with tf.device(self._get_device_setter(self._gpus[0]) if len(self._gpus) > 1 else None):
                if hasattr(model, 'metrics') and callable(model.metrics):
                    metrics = model.metrics()
                else:
                    metrics = self.metrics_getter(model)

            self._metrics = metrics

    def _setup_summary(self):
        model = self._towers_models[0]
        if hasattr(model, 'summary') and callable(model, 'summary'):
            ops = model.summary(self._step_var, self._learning_rate, self._grads)
        else:
            ops = self.summary_getter(
                model, self._step_var, self._learning_rate, self._grads, self._metrics)
        self.train_summary_op, self.valid_summary_op = ops
