'''
Processing data for learning procedures.
'''

import itertools
import json
import logging
import math
import os
from collections import namedtuple
from multiprocessing import Manager, cpu_count
from typing import Any, Dict, Iterable, List, Tuple, Union

import numpy as np
import pandas as pd
from pandas.util import hash_pandas_object
from sklearn.model_selection import train_test_split

from draco.learn.helper import count_violations, current_weights
from draco.spec import Data, Encoding, Field, Query, Task

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def absolute_path(p: str) -> str:
    return os.path.join(os.path.dirname(__file__), p)

pickle_path = absolute_path('../../__tmp__/data.pickle')
man_data_path = absolute_path('../../data/training/manual.json')
yh_data_path = absolute_path('../../data/training/younghoon.json')
ba_data_path = absolute_path('../../data/training/bahador.json')
label_data_path = absolute_path('../../data/training/labeler.json')
compassql_data_path = absolute_path('../../data/compassql_examples')
data_dir = absolute_path('../../data/') # the dir containing data used in visualization

halden_data_path = absolute_path('../../data/to_label')


PosNegExample = namedtuple('PosNeg', ['pair_id', 'data', 'task', 'source', 'negative', 'positive'])
UnlabeledExample = namedtuple('Unlabeled', ['pair_id', 'data', 'task', 'source', 'left', 'right'])


def load_neg_pos_specs() -> List[PosNegExample]:
    raw_data = []

    for path in [man_data_path, yh_data_path, ba_data_path, label_data_path]:
        with open(path) as f:
            i = 0
            json_data = json.load(f)

            for row in json_data['data']:
                fields = list(map(Field.from_obj, row['fields']))
                spec_schema = Data(fields, row.get('num_rows'))
                src = json_data['source']
                raw_data.append(PosNegExample(
                    f'{src}-{i}',
                    spec_schema,
                    row.get('task'),
                    src,
                    row['negative'],
                    row['positive'])
                )

                i += 1

    return raw_data


def load_partial_full_data(path=compassql_data_path, data_dir=data_dir):
    ''' load partial-full spec pairs from the directory
        Args:
            compassql_data_dir: the directory containing compassql data with
                 'input' and 'output' directories specifying compassql input and output
        Returns:
            A dictionary mapping each case name into a pair of partial spec - full spec.
    '''

    def load_spec(input_dir, data_format: str):
        ''' load compassql data
            Args: input_dir: the directory containing a set of json compassql specs
                  data_format: one of 'compassql' and 'vegalite'
            Returns:
                a dictionary containing name and the Task object representing the spec
        '''
        files = [os.path.join(input_dir, f) for f in os.listdir(input_dir)]
        result = {}
        for fname in files:
            if not fname.endswith('.json'):
                continue
            with open(fname, 'r') as f:
                content = json.load(f)
                if 'url' in content['data'] and content['data']['url'] is not None:
                    content['data']['url'] = os.path.join(data_dir, os.path.basename(content['data']['url']))
                if data_format == 'compassql':
                    spec = Task.from_cql(content, '.')
                elif data_format == 'vegalite':
                    spec = Task.from_vegalite(content)
                    if spec.to_vegalite() != content:
                        logger.warning('Vega-Lite and spec from task are different')
                result[os.path.basename(fname)] = spec
        return result

    # TODO: do not parse and generate full VL specs or compass specs

    partial_specs = load_spec(os.path.join(path, 'input'), 'compassql')
    compassql_outs = load_spec(os.path.join(path, 'output'), 'vegalite')

    result = {}
    for k in partial_specs:
        result[k] = (partial_specs[k], compassql_outs[k])
    return result


def load_unlabeled_specs() -> List[UnlabeledExample]:
    files = [os.path.join(halden_data_path, f)
                for f in os.listdir(halden_data_path)
                if f.endswith('.json')]

    data_cache = {}
    def acquire_data(url):
        if url not in data_cache:
            data_cache[url] = Data.from_json(os.path.join(data_dir, os.path.basename(url)))
            # set the url to short name, since the one above set it to full name in the current machine
            data_cache[url].url = url
        return data_cache[url]

    raw_data: List[UnlabeledExample] = []

    cnt = 0

    for fname in files:
        with open(fname, 'r') as f:
            content = json.load(f)
            for num_channel in content:
                for spec_list in content[num_channel]:
                    for left, right in itertools.combinations(spec_list, 2):
                        assert left != right, '[Err] find pairs with the same content file:{} - num_channel:{} - group:{}'.format(os.path.basename(fname), num_channel, i)
                        assert left['data']['url'] == right['data']['url']

                        url = left["data"]["url"]

                        raw_data.append(UnlabeledExample(
                            f'halden-{cnt}',
                            None,
                            acquire_data(url),
                            'halden',
                            left,
                            right
                        ))
                        cnt += 1

    return raw_data


def count_violations_memoized(processed_specs: Dict[str, Dict], task: Task):
    key = task.to_asp()
    if key not in processed_specs:
        processed_specs[key] = count_violations(task)
    return processed_specs[key]


def get_nested_index():
    '''
    Gives you a nested pandas index that we apply to the data when creating a dataframe.
    '''
    features = get_feature_names()

    iterables = [['negative', 'positive'], features]
    index = pd.MultiIndex.from_product(iterables, names=['category', 'feature'])
    index = index.append(pd.MultiIndex.from_arrays([['source', 'task'], ['', '']]))
    return index


def get_feature_names():
    weights = current_weights()
    features = sorted(map(lambda s: s[:-len('_weight')], weights.keys()))

    return features


def pair_partition_to_vec(input_data: Tuple[Dict, Iterable[Union[PosNegExample, UnlabeledExample, np.ndarray]]]):
    processed_specs, partiton_data = input_data

    columns = get_nested_index()
    dfs = []

    for example in partiton_data:
        Encoding.encoding_cnt = 0

        if isinstance(example, np.ndarray):
            example = PosNegExample(*example)

        neg_feature_vec = count_violations_memoized(processed_specs,
                            Task(example.data, Query.from_vegalite(example.negative), example.task))
        pos_feature_vec = count_violations_memoized(processed_specs,
                            Task(example.data, Query.from_vegalite(example.positive), example.task))

        # Reformat the json data so that we can insert it into a multi index data frame.
        # https://stackoverflow.com/questions/24988131/nested-dictionary-to-multiindex-dataframe-where-dictionary-keys-are-column-label
        specs = {('negative', key): values for key, values in neg_feature_vec.items()}
        specs.update({('positive', key): values for key, values in pos_feature_vec.items()})

        specs[('source', '')] = example.source
        specs[('task', '')] = example.task

        dfs.append(pd.DataFrame(specs, columns=columns, index=[example.pair_id]))

    return pd.concat(dfs)


def run_in_parallel(func, data: List[Any]) -> pd.DataFrame:
    ''' Like map, but parallel. '''

    splits = min([cpu_count() * 20, math.ceil(len(data) / 10)])
    df_split = np.array_split(data, splits)
    processes = min(cpu_count(), splits)

    logger.info(f'Running {splits} partitions of {len(data)} items in parallel on {processes} processes.')

    with Manager() as manager:
        m: Any = manager  # fix for mypy
        d = m.dict()  # shared dict for memoization
        pool = m.Pool(processes=processes)
        df = pd.concat(pool.map(func, list(map(lambda s: (d,s), df_split))))
        pool.close()
        pool.join()

    df = df.sort_index()

    logger.info(f'Hash of dataframe: {hash_pandas_object(df).sum()}')

    return df


def pairs_to_vec(specs: List[Union[PosNegExample, UnlabeledExample]]) -> pd.DataFrame:
    ''' given specs, convert them into feature vectors. '''

    return run_in_parallel(pair_partition_to_vec, specs)


def _get_pos_neg_data() -> pd.DataFrame:
    '''
    Internal function to load the feature vecors.
    '''
    data = pd.read_pickle(pickle_path)
    data.fillna(0, inplace=True)

    return data


def load_data(test_size: float=0.3, random_state=1) -> Tuple[pd.DataFrame, pd.DataFrame]:
    '''
        Returns:
            a tuple containing: train_dev, test.
    '''
    data = _get_pos_neg_data()
    return train_test_split(data, test_size=test_size, random_state=random_state)



def get_labeled_data() -> Tuple[List[PosNegExample], pd.DataFrame]:
    specs = load_neg_pos_specs()
    vecs = _get_pos_neg_data()

    assert len(specs) == len(vecs)

    return specs, vecs


def get_unlabeled_data() -> Tuple[List[UnlabeledExample], pd.DataFrame]:
    specs = load_unlabeled_specs()
    vecs = pairs_to_vec(specs)

    assert len(specs) == len(vecs)

    return specs, vecs


if __name__ == '__main__':
    ''' Generate and store vectors for labeled data in default path. '''
    neg_pos_data = load_neg_pos_specs()
    data = pairs_to_vec(neg_pos_data)
    data.to_pickle(pickle_path)
