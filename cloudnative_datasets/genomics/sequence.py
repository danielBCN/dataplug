import re
from typing import BinaryIO, Tuple, Dict

from ..compressed.gzipped import GZippedText
from ..cobase import CloudObjectBase

from ..preprocessers import MapReducePreprocesser


class FASTQGZip(GZippedText):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class FASTA(CloudObjectBase):
    def preprocess(self, object_stream: BinaryIO) -> Tuple[bytes, Dict[str, str]]:
        pass


class FASTAPreprocesser(MapReducePreprocesser):
    def __init__(self):
        super().__init__()

    @staticmethod
    def __get_length(min_range, content, data, start_base, end_base):
        start_base -= min_range
        end_base -= min_range
        len_base = len(data[start_base:end_base].replace('\n', ''))
        # name_id num_chunks_has_divided offset_head offset_bases ->
        # name_id num_chunks_has_divided offset_head offset_bases len_bases
        content[-1] = f'{content[-1]} {len_base}'

    @staticmethod
    def map(data_stream, worker_id, key, chunk_size, obj_size, partitions):
        min_range = worker_id * chunk_size
        max_range = int(obj_size) if worker_id == partitions - 1 else (worker_id + 1) * chunk_size
        # data = self.storage.get_object(bucket=self.bucket, key=key,
        #                                extra_get_args={'Range': f'bytes={min_range}-{max_range - 1}'}).decode('utf-8')
        data = data_stream.read().decode('utf-8')

        content = []
        # If it were '>' it would also find the ones inside the head information
        ini_heads = list(re.finditer(r"\n>", data))
        heads = list(re.finditer(r">.+\n", data))

        # If the list is not empty or there is > in the first byte (if is empty it will return an empty list)
        if ini_heads or data[0] == '>':
            first_sequence = True
            prev = -1
            for m in heads:
                start = min_range + m.start()
                end = min_range + m.end()
                if first_sequence:
                    first_sequence = False
                    if worker_id > 0 and start - 1 > min_range:
                        # If it is not the worker of the first part of the file and in addition it
                        # turns out that the partition begins in the middle of the base of a sequence.
                        # (start-1): avoid having a split sequence in the index that only has '\n'
                        match_text = list(re.finditer('.*\n', data[0:m.start()]))
                        if match_text:
                            text = match_text[0].group().split(' ')[0]
                            length_0 = len(data[match_text[0].start():m.start()].replace('\n', ''))
                            offset_0 = match_text[0].start() + min_range
                            if len(match_text) > 1:
                                offset_1 = match_text[1].start() + min_range
                                length_1 = len(data[match_text[1].start():m.start()].replace('\n', ''))
                                length_base = f"{length_0}-{length_1}"
                                offset = f"{offset_0}-{offset_1}"
                            else:
                                length_base = f"{length_0}"
                                offset = f'{offset_0}'
                            # >> num_chunks_has_divided offset_head offset_bases_split length/s
                            # first_line_before_space_or_\n
                            content.append(f">> <X> <Y> {str(offset)} {length_base} ^{text}^")  # Split sequences
                        else:
                            # When the first header found is false, when in a split stream there is a split header
                            # that has a '>' inside (ex: >tr|...o-alpha-(1->5)-L-e...\n)
                            first_sequence = True
                            start = end = -1  # Avoid entering the following condition
                if prev != start:
                    # When if the current sequence base is not empty
                    if prev != -1:
                        FASTAPreprocesser.__get_length(min_range, content, data, prev, start)
                    # name_id num_chunks_has_divided offset_head offset_bases
                    content.append(f"{m.group().split(' ')[0].replace('>', '')} 1 {str(start)} {str(end)}")
                prev = end

            len_head = len(heads)
            if ini_heads[-1].start() + 1 > heads[-1].start():
                # Check if the last head of the current one is cut. (ini_heads[-1].start() + 1): ignore '\n'
                last_seq_start = ini_heads[-1].start() + min_range + 1  # (... + 1): ignore '\n'
                if len_head != 0:
                    # Add length of bases to last sequence
                    FASTAPreprocesser.__get_length(min_range, content, data, prev, last_seq_start)
                text = data[last_seq_start - min_range::]
                # [<->|<_>]name_id_split offset_head
                content.append(f"{'<-' if ' ' in text else '<_'}{text.split(' ')[0]} {str(last_seq_start)}")
                # if '<->' there is all id
            elif len_head != 0:
                # Add length of bases to last sequence
                FASTAPreprocesser.__get_length(min_range, content, data, prev, max_range)

        return {'min_range': min_range,
                'max_range': max_range,
                'sequences': content}

    @staticmethod
    def reduce(results):
        if len(results) > 1:
            for i, dict in enumerate(results):
                dictio = dict['sequences']
                dict_prev = results[i - 1]
                seq_range_prev = dict_prev['sequences']
                if i > 0 and seq_range_prev and dictio and '>>' in dictio[
                    0]:  # If i > 0 and not empty the current and previous dictionary and the first sequence is split
                    param = dictio[0].split(' ')
                    seq_prev = seq_range_prev[-1]
                    param_seq_prev = seq_prev.split(' ')
                    if '<->' in seq_prev or '<_>' in seq_prev:
                        if '<->' in seq_range_prev[-1]:  # If the split was after a space, then there is all id
                            name_id = param_seq_prev[0].replace('<->', '')
                        else:
                            name_id = param_seq_prev[0].replace('<_>', '') + param[5].replace('^', '')
                        length = param[4].split('-')[1]
                        offset_head = param_seq_prev[1]
                        offset_base = param[3].split('-')[1]
                        seq_range_prev.pop()  # Remove previous sequence
                        split = 0
                        # Update ranges
                        dict['min_range'] = int(offset_head)
                        dict_prev['max_range'] = int(offset_head)
                    else:
                        length = param[4].split('-')[0]
                        name_id = param_seq_prev[0]
                        offset_head = param_seq_prev[2]
                        offset_base = param[3].split('-')[0]
                        split = int(param_seq_prev[1])
                        # Update number of partitions of the sequence
                        for x in range(i - split, i):  # Update previous sequences
                            results[x]['sequences'][-1] = results[x]['sequences'][-1].replace(
                                f' {split} ',
                                f' {split + 1} ')  # num_chunks_has_divided + 1 (i+1: total of current partitions of sequence)
                    dictio[0] = dictio[0].replace(f' {param[5]}', '')  # Remove 4rt param
                    dictio[0] = dictio[0].replace(f' {param[3]} ',
                                                  f' {offset_base} ')  # [offset_base_0-offset_base_1|offset_base] -> offset_base
                    dictio[0] = dictio[0].replace(f' {param[4]}', f' {length}')  # [length_0-length_1|length] -> length
                    dictio[0] = dictio[0].replace(' <X> ', f' {str(split + 1)} ')  # X --> num_chunks_has_divided
                    dictio[0] = dictio[0].replace(' <Y> ', f' {offset_head} ')  # Y --> offset_head
                    dictio[0] = dictio[0].replace('>> ', f'{name_id} ')  # '>>' -> name_id
