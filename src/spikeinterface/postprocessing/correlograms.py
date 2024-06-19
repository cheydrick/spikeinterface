from __future__ import annotations
import math
import warnings
import numpy as np
from spikeinterface.core.sortinganalyzer import register_result_extension, AnalyzerExtension, SortingAnalyzer

from spikeinterface.core.waveforms_extractor_backwards_compatibility import MockWaveformExtractor

try:
    import numba

    HAVE_NUMBA = True
except ModuleNotFoundError as err:
    HAVE_NUMBA = False

# TODO: here the default is 50 ms but in the docs it says 100 ms?

# _set_params, _select_extension_data, _run, _get_data I think are
# sorting analyzer things. Docstrings can be added here and propagated to
# all sorting analyer functions OR can be described in the class docstring.
# otherwise these are quite hard to understand where they are called in the
# code as not called internally on the class.

# compute_autocorrelogram_from_spiketrain
# TODO: in another PR, coerce this input into `correlogram_for_one_segment()`
# to provide a numpy and numba version. Consider window_size and bin_size
# being taken as ms to match general API.


class ComputeCorrelograms(AnalyzerExtension):
    """
    Compute auto and cross correlograms.

    In the extracellular electrophysiology context, a correlogram
    is a visualisation of the results of a cross-correlation
    between two spike trains. The cross-correlation slides one spike train
    along another sample-by-sample, taking the correlation at each 'lag'. This results
    in a plot with 'lag' (i.e. time offset) on the x-axis and 'correlation'
    (i.e. how similar to two spike trains are) on the y-axis. In this
    implementation, the y-axis result is the 'counts' of spike matches per
    time bin (rather than a computer correlation or covariance).

    Correlograms are often used to determine whether a unit has
    ISI violations. In this context, a 'window' around spikes is first
    specified. For example, if a window of 100 ms is taken, we will
    take the correlation at lags from -100 ms to +100 ms around the spike peak.
    In theory, we can have as many lags as we have samples. Often, this
    visualisation is too high resolution and instead the lags are binned
    (e.g. 0-5 ms, 5-10 ms, ..., 95-100 ms bins). When using counts as output,
    binning the lags involves adding up all counts across a range of lags.

    Parameters
    ----------
    sorting_analyzer: SortingAnalyzer
        A SortingAnalyzer object
    window_ms : float, default: 50.0
        The window around the spike to compute the correlation in ms. For example,  TODO: check this!
         if 50 ms, the correlations will be computed at tags -25 ms ... 25 ms.
    bin_ms : float, default: 1.0
        The bin size in ms. This determines the bin size over which to
         combine lags. For example, with a window size of -25 ms to 25 ms, and
         bin size 1 ms, the correlation will be binned as -25 ms, -24 ms, ...
    method : "auto" | "numpy" | "numba", default: "auto"
         If "auto" and numba is installed, numba is used, otherwise numpy is used.

    Returns
    -------
    correlogram : np.array
        Correlograms with shape (num_units, num_units, num_bins)
        The diagonal of correlogram is the auto correlogram. The output
        is in bin counts.
        correlogram[A, B, :] is the symetrie of correlogram[B, A, :]
        correlogram[A, B, :] have to be read as the histogram of spiketimesA - spiketimesB
    bins :  np.array
        The bin edges in ms
    """

    extension_name = "correlograms"
    depend_on = []
    need_recording = False
    use_nodepipeline = False
    need_job_kwargs = False

    def __init__(self, sorting_analyzer):
        AnalyzerExtension.__init__(self, sorting_analyzer)

    def _set_params(self, window_ms: float = 50.0, bin_ms: float = 1.0, method: str = "auto"):
        params = dict(window_ms=window_ms, bin_ms=bin_ms, method=method)

        return params

    def _select_extension_data(self, unit_ids):
        # filter metrics dataframe
        unit_indices = self.sorting_analyzer.sorting.ids_to_indices(unit_ids)
        new_ccgs = self.data["ccgs"][unit_indices][:, unit_indices]
        new_bins = self.data["bins"]
        new_data = dict(ccgs=new_ccgs, bins=new_bins)
        return new_data

    def _run(self, verbose=False):
        ccgs, bins = compute_correlograms_on_sorting(self.sorting_analyzer.sorting, **self.params)
        self.data["ccgs"] = ccgs
        self.data["bins"] = bins

    def _get_data(self):
        return self.data["ccgs"], self.data["bins"]


register_result_extension(ComputeCorrelograms)
compute_correlograms_sorting_analyzer = ComputeCorrelograms.function_factory()

# TODO: Question: what is the main entry functions for this module?
# is it only the below? If so can all other functions be made private?
# This would reduce some docstring duplication


def compute_correlograms(
    sorting_analyzer_or_sorting,
    window_ms: float = 50.0,
    bin_ms: float = 1.0,
    method: str = "auto",
):
    """
    Compute correlograms using Numba or Numpy.
    See ComputeCorrelograms() for details.
    """
    if isinstance(sorting_analyzer_or_sorting, MockWaveformExtractor):
        sorting_analyzer_or_sorting = sorting_analyzer_or_sorting.sorting

    if isinstance(sorting_analyzer_or_sorting, SortingAnalyzer):
        return compute_correlograms_sorting_analyzer(
            sorting_analyzer_or_sorting, window_ms=window_ms, bin_ms=bin_ms, method=method
        )
    else:
        return compute_correlograms_on_sorting(
            sorting_analyzer_or_sorting, window_ms=window_ms, bin_ms=bin_ms, method=method
        )


compute_correlograms.__doc__ = compute_correlograms_sorting_analyzer.__doc__


def _make_bins(sorting, window_ms, bin_ms):
    """
    Create the bins for the autocorrelogram, in samples.

    The autocorrelogram bins are centered around zero but do not
    include the results from zero lag. Each bin increases in
    a positive / negative direction starting at zero.

    For example, given a window_ms of 50 ms and a bin_ms of
    5 ms, the bins in unit ms will be:
    [-25 to -20, ..., -5 to 0, 0 to 5, ..., 20 to 25].

    The window size will be clipped if not divisible by the bin size.
    The bins are output in sample units, not seconds.

    Parameters
    ----------
    See ComputeCorrelograms() for parameters.

    Returns
    -------

    bins : np.ndarray
        The bins edges in ms
    window_size : int
        The window size in samples
    bin_size : int
        The bin size in samples

    """
    fs = sorting.sampling_frequency

    window_size = int(round(fs * window_ms / 2 * 1e-3))
    bin_size = int(round(fs * bin_ms * 1e-3))
    window_size -= window_size % bin_size
    num_bins = 2 * int(window_size / bin_size)
    assert num_bins >= 1

    bins = np.arange(-window_size, window_size + bin_size, bin_size) * 1e3 / fs

    return bins, window_size, bin_size


def _compute_num_bins(window_size, bin_size):
    """
    Internal function to compute number of bins, expects
    window_size and bin_size are already divisible and
    typically generated in `_make_bins()`.

    Returns
    -------
    num_bins : int
        The total number of bins to span the window, in samples
    half_num_bins : int
        Half the number of bins. The bins are an equal number
        of bins that look forward and backwards from zero, e.g.
        [..., -10 to -5, -5 to 0, 0 to 5, 5 to 10, ...]

    """
    num_half_bins = int(window_size // bin_size)
    num_bins = int(2 * num_half_bins)

    return num_bins, num_half_bins


# TODO: this can now be deprecated as there is no distinction at the Numba level.
def compute_autocorrelogram_from_spiketrain(spike_times, window_size, bin_size):
    """
    Computes the auto-correlogram from a given spike train.

    This implementation only works if you have numba installed, to accelerate the
    computation time.

    Parameters
    ----------
    spike_times : np.ndarray
        The ordered spike train to compute the auto-correlogram.
    window_size : int
        Compute the auto-correlogram between -window_size and +window_size (in sampling time).
    bin_size : int
        Size of a bin (in sampling time).
    Returns
    -------
    auto_corr : np.ndarray[int64]
        The computed auto-correlogram.
    bins :
    """
    assert HAVE_NUMBA
    return _compute_correlograms_one_segment_numba(spike_times.astype(np.int64, copy=False), window_size, bin_size)


# TODO: expose a numpy option also. UNless we want to force users to use `Sorting` or `SortingAnalyzer`.
# I am not averse to this, is helps reduce the suface API and assist maintaince. If users
# want to directly computer cross-correlograms they can use a private internal function.
# Thoughts?
def compute_crosscorrelogram_from_spiketrain(spike_times1, spike_times2, window_size, bin_size):
    """
    Computes the cros-correlogram between two given spike trains.

    This implementation only works if you have numba installed, to accelerate the
    computation time.

    Parameters
    ----------
    spike_times1: np.ndarray
        The ordered spike train to compare against the second one.
    spike_times2: np.ndarray
        The ordered spike train that serves as a reference for the cross-correlogram.
    window_size: int
        Compute the auto-correlogram between -window_size and +window_size (in sampling time).
    bin_size: int
        Size of a bin (in sampling time).

    Returns
    -------
    tuple (auto_corr, bins)
    auto_corr: np.ndarray[int64]
        The computed auto-correlogram.
    """
    assert HAVE_NUMBA
    return _compute_correlograms_one_segment_numba(
        spike_times1.astype(np.int64), spike_times2.astype(np.int64, copy=False), window_size, bin_size
    )


def compute_correlograms_on_sorting(sorting, window_ms, bin_ms, method="auto"):
    """
    Computes several cross-correlogram in one course from several clusters.

    Entry function to compute correlograms across all units in a `Sorting`
    object (i.e. spike trains at all determined offsets will be computed
    for each unit against every other unit).

    Parameters
    ----------
    sorting : Sorting
        A SpikeInterface Sorting object
    window_ms : int
            The window size over which to perform the cross-correlation, in ms
    bin_ms : int
        The size of which to bin lags, in ms.
    method : str
        To use "numpy" or "numba". "auto" will use numba if available,
        otherwise numpy.

    Returns
    -------
    correlograms : np.array
        A (num_units, num_units, num_bins) array where unit x unit correlation
        matrices are stacked at all determined time bins. Note the true
        correlation is not returned but instead the count of number of matches.
    bins : np.array
        The bins edges in ms
    """
    assert method in ("auto", "numba", "numpy")

    if method == "auto":
        method = "numba" if HAVE_NUMBA else "numpy"

    bins, window_size, bin_size = _make_bins(sorting, window_ms, bin_ms)

    if method == "numpy":
        correlograms = compute_correlograms_numpy(sorting, window_size, bin_size)
    if method == "numba":
        correlograms = compute_correlograms_numba(sorting, window_size, bin_size)

    return correlograms, bins


# LOW-LEVEL IMPLEMENTATIONS
def compute_correlograms_numpy(sorting, window_size, bin_size):
    """
    Computes cross-correlograms for all units in a sorting object.

    This very elegant implementation is copied from phy package written by Cyrille Rossant.
    https://github.com/cortex-lab/phylib/blob/master/phylib/stats/ccg.py

    The main modification is way the positive and negative are handled explicitly
    for rounding reasons.

    Other slight modifications have been made to fit the SpikeInterface
    data model (e.g. adding the ability to handle multiple segments).

    Adaptation: Samuel Garcia
    """
    num_seg = sorting.get_num_segments()
    num_units = len(sorting.unit_ids)
    spikes = sorting.to_spike_vector(concatenated=False)

    num_bins, num_half_bins = _compute_num_bins(window_size, bin_size)

    correlograms = np.zeros((num_units, num_units, num_bins), dtype="int64")

    for seg_index in range(num_seg):
        spike_times = spikes[seg_index]["sample_index"]
        spike_labels = spikes[seg_index]["unit_index"]

        c0 = correlogram_for_one_segment(spike_times, spike_labels, window_size, bin_size)

        correlograms += c0

    return correlograms


def correlogram_for_one_segment(spike_times, spike_labels, window_size, bin_size):
    """
    A very well optimized algorithm for the cross-correlation of
    spike trains, copied from the Phy package, written by Cyrille Rossant.

    For all spikes, time difference between this spike and
    every other spike within the window is directly computed
    and stored as a count in the relevant lag time bin.

    Initially, the spike_times array is shifted by 1 position, and the difference
    computed. This gives the time differences betwen the closest spikes
    (skipping the zero-lag case). Next, the differences between
    spikes times in samples are converted into units relative to
    bin_size ('binarized'). Spikes in which the binarized difference to
    their closest neighbouring spike is greater than half the bin-size are
    masked and not compared in future.

    Finally, the indicies of the (num_units, num_units, num_bins) correlogram
    that need incrementing are done so with `ravel_multi_index()`. This repeats
    for all shifts along the spike_train until no spikes have a corresponding
    match within the window size.

    Parameters
    ----------
    spike_times : np.ndarray
        An array of spike times (in samples, not seconds).
        This contains spikes from all units.
    spike_labels : np.ndarray
        An array of labels indicating the unit of the corresponding
        spike in `spike_times`.
    window_size : int
        The window size over which to perform the cross-correlation, in samples
    bin_size : int
        The size of which to bin lags, in samples.
    """
    num_bins, num_half_bins = _compute_num_bins(window_size, bin_size)
    num_units = len(np.unique(spike_labels))

    correlograms = np.zeros((num_units, num_units, num_bins), dtype="int64")

    # At a given shift, the mask precises which spikes have matching spikes
    # within the correlogram time window.
    mask = np.ones_like(spike_times, dtype="bool")

    # The loop continues as long as there is at least one spike with
    # a matching spike.
    shift = 1
    while mask[:-shift].any():
        # Number of time samples between spike i and spike i+shift.
        spike_diff = spike_times[shift:] - spike_times[:-shift]

        for sign in (-1, 1):
            # Binarize the delays between spike i and spike i+shift for negative and positive
            # the operator // is np.floor_divide
            spike_diff_b = (spike_diff * sign) // bin_size

            # Spikes with no matching spikes are masked.
            if sign == -1:
                mask[:-shift][spike_diff_b < -num_half_bins] = False
            else:
                # spike_diff_b[np.where(spike_diff_b == num_half_bins)] -= 1  adds to the first AND last bin, which we dont want.
                mask[:-shift][spike_diff_b >= num_half_bins] = False
                # spike_diff_b[spike_diff_b == num_half_bins] = 0  # fills the central bin
                # the problem is that we need to mask specific pairs of comparisons
                # but this is just masking the entire spike time.
                # I still don't understand why removing the bound is leading to error at all,
                # it is leading to extra counting.

            m = mask[:-shift]

            # Find the indices in the raveled correlograms array that need
            # to be incremented, taking into account the spike clusters.
            if sign == 1:
                indices = np.ravel_multi_index(
                    (spike_labels[+shift:][m], spike_labels[:-shift][m], spike_diff_b[m] + num_half_bins),
                    correlograms.shape,
                )
            else:
                indices = np.ravel_multi_index(
                    (spike_labels[:-shift][m], spike_labels[+shift:][m], spike_diff_b[m] + num_half_bins),
                    correlograms.shape,
                )

            # Increment the matching spikes in the correlograms array.
            bbins = np.bincount(indices)
            correlograms.ravel()[: len(bbins)] += bbins

        shift += 1

    return correlograms


def compute_correlograms_numba(sorting, window_size, bin_size):
    """
    Computes cross-correlograms between all units in `sorting`.

    This is a "brute force" method using compiled code (numba)
    to accelerate the computation. See
    `_compute_correlograms_one_segment_numba()` for details.

    Parameters
    ----------
    sorting : Sorting
        A SpikeInterface Sorting object
    window_size : int
            The wi  ndow size over which to perform the cross-correlation, in samples
    bin_size : int
        The size of which to bin lags, in samples.

    Returns
    -------
    correlograms: np.array
        A (num_units, num_units, num_bins) array of correlograms
        between all units at each lag time bin.

    Implementation: Aurélien Wyngaard
    """
    assert HAVE_NUMBA, "numba version of this function requires installation of numba"

    num_bins, num_half_bins = _compute_num_bins(window_size, bin_size)
    num_units = len(sorting.unit_ids)

    spikes = sorting.to_spike_vector(concatenated=False)
    correlograms = np.zeros((num_units, num_units, num_bins), dtype=np.int64)

    for seg_index in range(sorting.get_num_segments()):
        spike_times = spikes[seg_index]["sample_index"]
        spike_labels = spikes[seg_index]["unit_index"]

        _compute_correlograms_numba(
            correlograms,
            spike_times.astype(np.int64, copy=False),
            spike_labels.astype(np.int32, copy=False),
            window_size,
            bin_size,
        )

        if False:
            _compute_correlograms_one_segment_numba(
                correlograms,
                spike_times.astype(np.int64, copy=False),
                spike_labels.astype(np.int32, copy=False),
                window_size,
                bin_size,
                num_half_bins,
            )

    return correlograms


if HAVE_NUMBA:

    #   @numba.jit(
    #      nopython=True,
    #     nogil=True,
    #    cache=False,
    # )
    def _compute_correlograms_one_segment_numba(
        correlograms, spike_times, spike_labels, window_size, bin_size, num_half_bins
    ):
        """
        Compute the correlograms using `numba` for speed.

        The algorithm works by brute-force iteration through all
        pairs of spikes (skipping those when outside of the window).
        The spike-time difference and its time bin are computed
        and stored in a (num_units, num_units, num_bins)
        correlogram. The correlogram must be passed as an
        argument and is filled in-place.

        Paramters
        ---------

        correlograms: np.array
            A (num_units, num_units, num_bins) array of correlograms
            between all units at each lag time bin. This is passed
            as counts for all segments are added to it.
        spike_times : np.ndarray
            An array of spike times (in samples, not seconds).
            This contains spikes from all units.
        spike_labels : np.ndarray
            An array of labels indicating the unit of the corresponding
            spike in `spike_times`.
        window_size : int
            The window size over which to perform the cross-correlation, in samples
        bin_size : int
            The size of which to bin lags, in samples.
        """
        start_j = 0
        for i in range(spike_times.size):
            for j in range(start_j, spike_times.size):

                if i == j:
                    continue

                diff = spike_times[i] - spike_times[j]

                # if the time of spike i is more than window size later than
                # spike j, then spike i + 1 will also be more than a window size
                # later than spike j. Iterate the start_j and check the next spike.
                if diff == window_size:
                    continue

                if diff > window_size:
                    start_j += 1
                    continue

                # If the time of spike i is more than a window size earlier
                # than spike j, then all following j spikes will be even later
                # i spikes and so all more than a window size earlier. So move
                # onto the next i.
                if diff < -window_size:
                    break

                bin = diff // bin_size

                correlograms[spike_labels[i], spike_labels[j], num_half_bins + bin] += 1

    # -----------------------------------------------------------------------------
    # To Deprecate
    # -----------------------------------------------------------------------------

    @numba.jit(nopython=True, nogil=True, cache=False)
    def _compute_autocorr_numba(spike_times, window_size, bin_size):
        num_half_bins = window_size // bin_size
        num_bins = 2 * num_half_bins

        auto_corr = np.zeros(num_bins, dtype=np.int64)

        for i in range(len(spike_times)):
            for j in range(i + 1, len(spike_times)):
                diff = spike_times[j] - spike_times[i]

                if diff > window_size:
                    break

                bin = int(math.floor(diff / bin_size))
                # ~ auto_corr[num_bins//2 - bin - 1] += 1
                auto_corr[num_half_bins + bin] += 1
                # ~ print(diff, bin, num_half_bins + bin)

                bin = int(math.floor(-diff / bin_size))
                auto_corr[num_half_bins + bin] += 1
                # ~ print(diff, bin, num_half_bins + bin)

        return auto_corr

    @numba.jit(nopython=True, nogil=True, cache=False)
    def _compute_crosscorr_numba(spike_times1, spike_times2, window_size, bin_size):
        num_half_bins = window_size // bin_size
        num_bins = 2 * num_half_bins

        cross_corr = np.zeros(num_bins, dtype=np.int64)

        start_j = 0
        for i in range(len(spike_times1)):
            for j in range(start_j, len(spike_times2)):
                diff = spike_times1[i] - spike_times2[j]

                if diff >= window_size:
                    start_j += 1
                    continue
                if diff < -window_size:
                    break

                bin = int(math.floor(diff / bin_size))
                # ~ bin = diff // bin_size
                cross_corr[num_half_bins + bin] += 1
                # ~ print(diff, bin, num_half_bins + bin)

        return cross_corr

    @numba.jit(
        nopython=True,
        nogil=True,
        cache=False,
        parallel=True,
    )
    def _compute_correlograms_numba(correlograms, spike_times, spike_labels, window_size, bin_size):
        n_units = correlograms.shape[0]

        for i in numba.prange(n_units):
            # ~ for i in range(n_units):
            spike_times1 = spike_times[spike_labels == i]

            for j in range(i, n_units):
                spike_times2 = spike_times[spike_labels == j]

                if i == j:
                    correlograms[i, j, :] += _compute_autocorr_numba(spike_times1, window_size, bin_size)
                else:
                    cc = _compute_crosscorr_numba(spike_times1, spike_times2, window_size, bin_size)
                    correlograms[i, j, :] += cc
                    correlograms[j, i, :] += cc[::-1]
