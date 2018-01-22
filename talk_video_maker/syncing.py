from concurrent.futures import ThreadPoolExecutor

import librosa
import numpy
import scipy

from . import objects, templates, videos
from .objects import hash_bytes, run
from .cdtw import dtw

SAMPLE_RATE = 22050
DTW_HOP_RATIO = 3/4
DTW_CUTOFF = 1/8
STFT_HOP_LENGTH = 512
DTW_WINDOW_LENGTH = 240  # seconds

DTW_WINDOW_SIZE = DTW_WINDOW_LENGTH * SAMPLE_RATE // STFT_HOP_LENGTH

thread_executor = ThreadPoolExecutor(1)  # XXX: higher value


def get_audio_offset(video_a, video_b, max_stderr=1e-5, max_speed_error=1e-3):
    sync = SynchronizedObject(video_a, video_b)

    print(sync.filename)
    slope, intercept, r, stderr = sync.stats
    frames = intercept * 1
    frames_s = intercept * STFT_HOP_LENGTH / SAMPLE_RATE
    print('A is {}× faster than B'.format(slope))
    print('A is shifted by {} frames = {} s relative to B'.format(
        frames, frames_s))
    print('Speedup coefficient: {}'.format(r))
    print('Standard error of estimate: {}'.format(stderr))

    if stderr > max_stderr:
        raise ValueError('Audio sync: regression error too high')
    if abs(slope - 1) > max_speed_error:
        raise ValueError('Audio sync: Tracks have different speed')

    return intercept * STFT_HOP_LENGTH / SAMPLE_RATE

def offset_video(video_a, video_b, offset, mode='pad'):
    if mode == 'pad':
        result_a = _pad_video(video_a, 1, offset)
        result_b = _pad_video(video_b, -1, offset)
    elif mode == 'a':
        result_a = video_a
        result_b = _cut_video(video_b, -1, offset)
        if video_a.duration < result_b.duration:
            result_b = result_b.trimmed(end=video_a.duration)
    elif mode == 'b':
        result_a = _cut_video(video_a, 1, offset)
        if video_b.duration < result_a.duration:
            result_a = result_a.trimmed(end=video_b.duration)
        result_b = video_b
    else:
        raise ValueError('bad mode')
    return result_a, result_b

def _pad_video(video, side, offset):
    if offset * side <= 0:
        return video
    else:
        delay = side * offset
        blank = videos.BlankVideo(delay,
                                    width=video.width,
                                    height=video.height)
        return blank + video.faded_in(0.5)

def _cut_video(video, side, offset):
    if offset * side <= 0:
        delay = side * offset
        return video.trimmed(start=abs(delay))
    else:
        return _pad_video(video, side, offset)



class SynchronizedObject(objects.Object):
    ext = '.npy'

    def __init__(self, video_a, video_b):
        self.hash = hash_bytes(
            type(self).__name__.encode('utf-8'),
            video_a.hash.encode('utf-8'),
            video_b.hash.encode('utf-8'),
        )
        self.video_a = video_a
        self.video_b = video_b

    def save_to(self, filename):
        data = get_data(self.video_a, self.video_b)
        paths = get_wdwt_path(*data)
        with open(filename, 'wb') as f:
            numpy.save(f, paths)
        self._paths = paths

    @property
    def stats(self):
        self.save()
        try:
            paths = self._paths
        except AttributeError:
            with open(self.filename, 'rb') as f:
                paths = numpy.load(f)
        return regress(paths)


def get_data(video_a, video_b):
    opts = dict()

    def prepare_audio(video):
        return video.mono_audio().exported_audio('s16',
                                                 sample_rate=SAMPLE_RATE)

    def load_data(audio):
        signal, _sample_rate = librosa.load(audio.filename, sr=SAMPLE_RATE)
        mfcc = librosa.feature.mfcc(signal, SAMPLE_RATE, n_mfcc=10,
                                    hop_length=STFT_HOP_LENGTH)
        return signal, mfcc.T

    data_a, data_b = thread_executor.map(
        load_data, [prepare_audio(video_a), prepare_audio(video_b)])

    return data_a, data_b


def get_wdwt_path(data1, data2):
    y1, f1 = data1
    y2, f2 = data2
    path1 = [0]
    path2 = [0]
    path_chunk_length = int(DTW_WINDOW_SIZE * DTW_HOP_RATIO)
    while path1[-1] < len(f1) - 1 and path2[-1] < len(f2) - 1:
        start1, start2 = [int(n) for n in [path1[-1], path2[-1]]]
        print('Correlating... {}/{} {}/{} (~{}%), {} vs {}, sz {}'.format(
            len(path1), len(f1), len(path2), len(f2),
            int(min(len(path1)/len(f1), len(path2)/len(f2))*100),
            start1, start2, DTW_WINDOW_SIZE))
        dist, cost, path = dtw(f1[start1:start1+DTW_WINDOW_SIZE],
                            f2[start2:start2+DTW_WINDOW_SIZE])
        path1.extend(path[0][:path_chunk_length] + start1)
        path2.extend(path[1][:path_chunk_length] + start2)
    return numpy.array([path1, path2])


def regress(paths):
    length = paths.shape[1]
    cutoff = int(length * DTW_CUTOFF)
    slope, intercept, r, p, stderr = scipy.stats.linregress(
        paths[:,cutoff:-cutoff])
    return slope, intercept, r, stderr
