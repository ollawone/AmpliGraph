import numpy as np
from ..datasets import NumpyDatasetAdapter
import logging
import copy
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


class OneToNDatasetAdapter(NumpyDatasetAdapter):

    def __init__(self, low_memory=False):
        """Initialize the class variables
        """
        super(OneToNDatasetAdapter, self).__init__()

        self.filter_mapping = None
        self.filtered_status = {}
        self.output_mapping = None
        self.output_onehot = {}
        self.low_memory = low_memory
        self._subject_corruption_mode = False

    def set_filter(self, filter_triples, mapped_status=False):
        """ Set the filter to be used while generating an evaluation batch.

        Parameters
        ----------
        filter_triples : nd-array
            triples that would be used as filter
        """

        self.set_data(filter_triples, 'filter', mapped_status)
        self.filter_mapping = self.generate_output_mapping('filter')

    def generate_onehot_outputs(self, dataset_type='train', use_filter=False):
        """ Create one-hot outputs for a dataset using an output mapping.

        Parameters
        ----------
        dataset_type : string indicating which dataset to create onehot outputs for
        use_filter : bool indicating whether to use a filter when generating onehot outputs.

        Returns
        -------

        """

        if dataset_type not in self.dataset.keys():
            msg = 'Dataset `{}` not found: cannot generate one-hot outputs. ' \
                  'Please use `set_data` to set the dataset first.'.format(dataset_type)
            raise ValueError(msg)

        if use_filter:
            # Generate one-hot outputs using the filter
            if self.filter_mapping is None:
                msg = 'Filter not found: cannot generate one-hot outputs with `use_filter=True` ' \
                      'if a filter has not been set.'
                raise ValueError(msg)
            else:
                output_dict = self.filter_mapping
        else:
            # Generate one-hot outputs using the dataset key passed to set_output_mapping()
            if self.output_mapping is None:
                msg = 'Output mapping was not created before generating one-hot vectors. '
                raise ValueError(msg)
            else:
                output_dict = self.output_mapping

        if not self.low_memory:

            # Initialize np.array of shape [dataset_size, num_entities]
            self.output_onehot[dataset_type] = np.zeros((self.dataset[dataset_type].shape[0], len(self.ent_to_idx)),
                                                        dtype=np.int8)

            # Set one-hot indices using output_dict
            for i, x in enumerate(self.dataset[dataset_type]):
                indices = output_dict.get((x[0], x[1]), [])
                self.output_onehot[dataset_type][i, indices] = 1

            self.filtered_status[dataset_type] = use_filter

        else:
            # NB: With low_memory=True the output indices are generated on the fly in the batch yield function
            pass

    def generate_output_mapping(self, dataset_type='train'):
        """ Creates dictionary keyed on (subject, predicate) to list of objects

        Parameters
        ----------
        dataset_type: str

        Returns
        -------
            dict
        """

        # if data is not already mapped, then map before creating output map
        if not self.mapped_status[dataset_type]:
            self.map_data()

        output_mapping = dict()

        for s, p, o in self.dataset[dataset_type]:
            output_mapping.setdefault((s, p), []).append(o)

        return output_mapping

    def set_output_mapping(self, output_dict):
        """ Set the output mapping used to generate onehot vectors.

        Note: Setting a new output mapping will clear any previously generated onehot outputs, as otherwise can lead
        to a situation where old outputs are returned from batching function.

        Parameters
        ----------
        output_dict : dictionary of subject, predicate to object indices

        Returns
        -------

        """

        self.output_mapping = output_dict

        # Clear any onehot outputs previously generated
        self.output_onehot = {}

    def get_next_batch(self, batches_count=-1, dataset_type='train', use_filter=False):
        """Generator that returns the next batch of data.

        Parameters
        ----------
        batches_count: int
            number of batches per epoch (default: -1, i.e. uses batch_size of 1)
        dataset_type: string
            indicates which dataset to use
        use_filter : bool
            Flag to indicate whether to return the one-hot outputs are generated from filtered or unfiltered datasets
        Returns
        -------
        batch_output : nd-array
            A batch of triples from the dataset type specified
        batch_onehot : nd-array
            A batch of onehot arrays corresponding to `batch_output` triples
        """

        # if data is not already mapped, then map before returning the batch
        if not self.mapped_status[dataset_type]:
            self.map_data()

        if batches_count == -1:
            batch_size = 1
            batches_count = self.get_size(dataset_type)
        else:
            batch_size = int(np.ceil(self.get_size(dataset_type) / batches_count))

        if use_filter and self.filter_mapping is None:
            msg = 'Cannot set `use_filter=True` if a filter has not been set in the adapter. '
            raise ValueError(msg)

        if not self.low_memory:

            # If onehot outputs for dataset_type aren't initialized then create them, or
            # If using a filter, and the onehot outputs for dataset_type were previously generated without the filter
            if dataset_type not in self.output_onehot.keys() or (use_filter and not self.filtered_status[dataset_type]):
                self.generate_onehot_outputs(dataset_type, use_filter=use_filter)

            # Yield batches
            for i in range(batches_count):

                out = np.int32(self.dataset[dataset_type][(i * batch_size):((i + 1) * batch_size), :])
                out_onehot = self.output_onehot[dataset_type][(i * batch_size):((i + 1) * batch_size), :]

                yield out, out_onehot

        else:
            # Low-memory, generate one-hot outputs per batch on the fly
            if use_filter:
                output_dict = self.filter_mapping
            else:
                output_dict = self.output_mapping

            # Yield batches
            for i in range(batches_count):

                out = np.int32(self.dataset[dataset_type][(i * batch_size):((i + 1) * batch_size), :])

                out_onehot = np.zeros(shape=[out.shape[0], len(self.ent_to_idx)], dtype=np.int32)
                for j, x in enumerate(out):
                    indices = output_dict.get((x[0], x[1]), [])
                    out_onehot[j, indices] = 1

                yield out, out_onehot

    def get_next_batch_subject_corruptions(self, batch_size=-1, dataset_type='train', use_filter=True):
        """Batch generator for subject corruptions.

        To avoid multiple redundant forward-passes through the network, subject corruptions are performed once for
        each relation, and results accumulated for valid test triples.

        If there are no test triples for a relation, then that relation is ignored.

        Use batch_size to control memory usage (as a batch_size*N tensor will be allocated, where N is number
        of unique entities.)


        Parameters
        ----------
        batches_count: int
            Number of batches to return p
        dataset_type: string
            indicates which dataset to use
        use_filter : bool
            Flag to indicate whether to return the one-hot outputs are generated from filtered or unfiltered datasets

        Returns
        -------

        test_triples : nd-array
            A set of triples from the dataset type specified, that include the predicate currently returned in batch.
        batch_triples : nd-array of shape (N, 3), where N is number of unique entities.
            Batch of triples corresponding to one relationship, with all possible subject corruptions.
        batch_onehot : nd-array of shape (N, N), where N is number of unique entities.
            A batch of onehot arrays corresponding to the batch_triples output.

        """

        if use_filter:
            output_dict = self.filter_mapping
        else:
            output_dict = self.output_mapping

        if batch_size == -1:
            batch_size = self.get_size(dataset_type)

        ent_list = np.array(list(self.ent_to_idx.values()))
        rel_list = np.array(list(self.rel_to_idx.values()))

        for rel in rel_list:

            # Select test triples that have this relation
            rel_idx = self.dataset[dataset_type][:, 1] == rel
            test_triples = self.dataset[dataset_type][rel_idx]

            ent_idx = 0
            # If there are no test triples with this relation, ignore it   # NOTE: To have a tqdm progress bar, removing this requirement
            # if test_triples.shape[0] > 0:

            while ent_idx < len(ent_list):

                ents = ent_list[ent_idx:ent_idx+batch_size]
                ent_idx += batch_size

                # Note: the object column is just a dummy value so set to 0
                out = np.stack([ents, np.repeat(rel, len(ents)), np.repeat(0, len(ents))], axis=1)

                # Set one-hot filter
                out_filter = np.zeros((out.shape[0], len(ent_list)), dtype=np.int8)
                for j, x in enumerate(out):
                    indices = output_dict.get((x[0], x[1]), [])
                    out_filter[j, indices] = 1

                yield test_triples, out, out_filter


    def generate_negative_output_mappings(self):
        """Generates a list of negatives (s, p) and (o, p) that are not found in the dataset.

        Returns
        -------

        """

        A_mapping = self.A_adapter.output_mapping
        A_negatives = []

        for e in self.ent_to_idx.values():
            for r in self.rel_to_idx.values():
                x = (e, r)
                if not x in A_mapping.keys():
                    A_negatives.append(x)

        self.A_negatives = A_negatives

    def _validate_data(self, data):
        """Validates the data
        """
        if type(data) != np.ndarray:
            msg = 'Invalid type for input data. Expected ndarray, got {}'.format(type(data))
            raise ValueError(msg)

        if (np.shape(data)[1]) != 3:
            msg = 'Invalid size for input data. Expected number of column 3, got {}'.format(np.shape(data)[1])
            raise ValueError(msg)

    def set_data(self, dataset, dataset_type=None, mapped_status=False):
        """Set the dataset based on the type.
            Note: If you pass the same dataset type (which exists) it will be overwritten

        Parameters
        ----------
        dataset : nd-array or dictionary
            dataset of triples
        dataset_type : string
            if the dataset parameter is an nd- array then this indicates the type of the data being based
        mapped_status : bool
            indicates whether the data has already been mapped to the indices

        """
        if isinstance(dataset, dict):
            for key in dataset.keys():
                self._validate_data(dataset[key])
                self.dataset[key] = dataset[key]
                self.mapped_status[key] = mapped_status
        elif dataset_type is not None:
            self._validate_data(dataset)
            self.dataset[dataset_type] = dataset
            self.mapped_status[dataset_type] = mapped_status
        else:
            raise Exception("Incorrect usage. Expected a dictionary or a combination of dataset and it's type.")

        # If the concept-idx mappings are present, then map the passed dataset
        if not (len(self.rel_to_idx) == 0 or len(self.ent_to_idx) == 0):
            print('Mapping set data: {}'.format(dataset_type))
            self.map_data()
