"""Provide different loss or metrics classes for images."""
import tensorflow as tf

from deepreg.loss.util import NegativeLossMixin
from deepreg.loss.util import gaussian_kernel1d_size as gaussian_kernel1d
from deepreg.loss.util import rectangular_kernel1d, triangular_kernel1d
from deepreg.registry import REGISTRY

EPS = 1.0e-5


@REGISTRY.register_loss(name="ssd")
class SumSquaredDifference(tf.keras.losses.Loss):
    """
    Sum of squared distance between y_true and y_pred.

    y_true and y_pred have to be at least 1d tensor, including batch axis.
    """

    def __init__(
        self,
        reduction: str = tf.keras.losses.Reduction.SUM,
        name: str = "SumSquaredDifference",
    ):
        """
        Init.

        :param reduction: using SUM reduction over batch axis,
            calling the loss like `loss(y_true, y_pred)` will return a scalar tensor.
        :param name: name of the loss
        """
        super().__init__(reduction=reduction, name=name)

    def call(self, y_true: tf.Tensor, y_pred: tf.Tensor) -> tf.Tensor:
        """
        Return loss for a batch.

        :param y_true: shape = (batch, ...)
        :param y_pred: shape = (batch, ...)
        :return: shape = (batch,)
        """
        loss = tf.math.squared_difference(y_true, y_pred)
        loss = tf.keras.layers.Flatten()(loss)
        return tf.reduce_mean(loss, axis=1)


class GlobalMutualInformation(tf.keras.losses.Loss):
    """
    Differentiable global mutual information via Parzen windowing method.

    y_true and y_pred have to be at least 4d tensor, including batch axis.

    Reference: https://dspace.mit.edu/handle/1721.1/123142,
        Section 3.1, equation 3.1-3.5, Algorithm 1
    """

    def __init__(
        self,
        num_bins: int = 23,
        sigma_ratio: float = 0.5,
        reduction: str = tf.keras.losses.Reduction.SUM,
        name: str = "GlobalMutualInformation",
    ):
        """
        Init.

        :param num_bins: number of bins for intensity, the default value is empirical.
        :param sigma_ratio: a hyper param for gaussian function
        :param reduction: using SUM reduction over batch axis,
            calling the loss like `loss(y_true, y_pred)` will return a scalar tensor.
        :param name: name of the loss
        """
        super().__init__(reduction=reduction, name=name)
        self.num_bins = num_bins
        self.sigma_ratio = sigma_ratio

    def call(self, y_true: tf.Tensor, y_pred: tf.Tensor) -> tf.Tensor:
        """
        Return loss for a batch.

        :param y_true: shape = (batch, dim1, dim2, dim3)
            or (batch, dim1, dim2, dim3, ch)
        :param y_pred: shape = (batch, dim1, dim2, dim3)
            or (batch, dim1, dim2, dim3, ch)
        :return: shape = (batch,)
        """
        # adjust
        if len(y_true.shape) == 4:
            y_true = tf.expand_dims(y_true, axis=4)
            y_pred = tf.expand_dims(y_pred, axis=4)
        assert len(y_true.shape) == len(y_pred.shape) == 5

        # intensity is split into bins between 0, 1
        y_true = tf.clip_by_value(y_true, 0, 1)
        y_pred = tf.clip_by_value(y_pred, 0, 1)
        bin_centers = tf.linspace(0.0, 1.0, self.num_bins)  # (num_bins,)
        bin_centers = tf.cast(bin_centers, dtype=y_true.dtype)
        bin_centers = bin_centers[None, None, ...]  # (1, 1, num_bins)
        sigma = (
            tf.reduce_mean(bin_centers[:, :, 1:] - bin_centers[:, :, :-1])
            * self.sigma_ratio
        )  # scalar, sigma in the Gaussian function (weighting function W)
        preterm = 1 / (2 * tf.math.square(sigma))  # scalar
        batch, w, h, z, c = y_true.shape
        y_true = tf.reshape(y_true, [batch, w * h * z * c, 1])  # (batch, nb_voxels, 1)
        y_pred = tf.reshape(y_pred, [batch, w * h * z * c, 1])  # (batch, nb_voxels, 1)
        nb_voxels = y_true.shape[1] * 1.0  # w * h * z, number of voxels

        # each voxel contributes continuously to a range of histogram bin
        ia = tf.math.exp(
            -preterm * tf.math.square(y_true - bin_centers)
        )  # (batch, nb_voxels, num_bins)
        ia /= tf.reduce_sum(ia, -1, keepdims=True)  # (batch, nb_voxels, num_bins)
        ia = tf.transpose(ia, (0, 2, 1))  # (batch, num_bins, nb_voxels)
        pa = tf.reduce_mean(ia, axis=-1, keepdims=True)  # (batch, num_bins, 1)

        ib = tf.math.exp(
            -preterm * tf.math.square(y_pred - bin_centers)
        )  # (batch, nb_voxels, num_bins)
        ib /= tf.reduce_sum(ib, -1, keepdims=True)  # (batch, nb_voxels, num_bins)
        pb = tf.reduce_mean(ib, axis=1, keepdims=True)  # (batch, 1, num_bins)

        papb = tf.matmul(pa, pb)  # (batch, num_bins, num_bins)
        pab = tf.matmul(ia, ib)  # (batch, num_bins, num_bins)
        pab /= nb_voxels

        # MI: sum(P_ab * log(P_ab/P_ap_b))
        div = (pab + EPS) / (papb + EPS)
        return tf.reduce_sum(pab * tf.math.log(div + EPS), axis=[1, 2])

    def get_config(self) -> dict:
        """Return the config dictionary for recreating this class."""
        config = super().get_config()
        config["num_bins"] = self.num_bins
        config["sigma_ratio"] = self.sigma_ratio
        return config


@REGISTRY.register_loss(name="gmi")
class GlobalMutualInformationLoss(NegativeLossMixin, GlobalMutualInformation):
    """Revert the sign of GlobalMutualInformation."""


class LocalNormalizedCrossCorrelation(tf.keras.losses.Loss):
    """
    Local squared zero-normalized cross-correlation.

    Denote y_true as t and y_pred as p. Consider a window having n elements.
    Each position in the window corresponds a weight w_i for i=1:n.

    Define the discrete expectation in the window E[t] as

        E[t] = sum_i(w_i * t_i) / sum_i(w_i)

    Similarly, the discrete variance in the window V[t] is

        V[t] = E[t**2] - E[t] ** 2

    The local squared zero-normalized cross-correlation is therefore

        E[ (t-E[t]) * (p-E[p]) ] ** 2 / V[t] / V[p]

    where the expectation in numerator is

        E[ (t-E[t]) * (p-E[p]) ] = E[t * p] - E[t] * E[p]

    Different kernel corresponds to different weights.

    For now, y_true and y_pred have to be at least 4d tensor, including batch axis.

    Reference:

        - Zero-normalized cross-correlation (ZNCC):
            https://en.wikipedia.org/wiki/Cross-correlation
        - Code: https://github.com/voxelmorph/voxelmorph/blob/legacy/src/losses.py
    """

    kernel_fn_dict = dict(
        gaussian=gaussian_kernel1d,
        rectangular=rectangular_kernel1d,
        triangular=triangular_kernel1d,
    )

    def __init__(
        self,
        kernel_size: int = 9,
        kernel_type: str = "rectangular",
        reduction: str = tf.keras.losses.Reduction.SUM,
        name: str = "LocalNormalizedCrossCorrelation",
    ):
        """
        Init.

        :param kernel_size: int. Kernel size or kernel sigma for kernel_type='gauss'.
        :param kernel_type: str, rectangular, triangular or gaussian
        :param reduction: using SUM reduction over batch axis,
            calling the loss like `loss(y_true, y_pred)` will return a scalar tensor.
        :param name: name of the loss
        """
        super().__init__(reduction=reduction, name=name)
        if kernel_type not in self.kernel_fn_dict.keys():
            raise ValueError(
                f"Wrong kernel_type {kernel_type} for LNCC loss type. "
                f"Feasible values are {self.kernel_fn_dict.keys()}"
            )
        self.kernel_fn = self.kernel_fn_dict[kernel_type]
        self.kernel_type = kernel_type
        self.filters = tf.ones(
            shape=[self.kernel_size, self.kernel_size, self.kernel_size, 1, 1]
        )
        self.strides = [1, 1, 1, 1, 1]
        self.padding = "SAME"

        # E[1] = sum_i(w_i), ()
        self.kernel_vol = kernel_size ** 3

    def call(self, y_true: tf.Tensor, y_pred: tf.Tensor) -> tf.Tensor:
        """
        Return loss for a batch.

        :param y_true: shape = (batch, dim1, dim2, dim3)
            or (batch, dim1, dim2, dim3, ch)
        :param y_pred: shape = (batch, dim1, dim2, dim3)
            or (batch, dim1, dim2, dim3, ch)
        :return: shape = (batch,)
        """
        # adjust
        if len(y_true.shape) == 4:
            y_true = tf.expand_dims(y_true, axis=4)
            y_pred = tf.expand_dims(y_pred, axis=4)
        assert len(y_true.shape) == len(y_pred.shape) == 5

        # t = y_true, p = y_pred
        # (batch, dim1, dim2, dim3, ch)
        t2 = y_true * y_true
        p2 = y_pred * y_pred
        tp = y_true * y_pred

        # sum over kernel
        # (batch, dim1, dim2, dim3, 1)
        t_sum = tf.nn.conv3d(
            y_true, filters=self.filters, strides=self.strides, padding=self.padding
        )
        p_sum = tf.nn.conv3d(
            y_pred, filters=self.filters, strides=self.strides, padding=self.padding
        )
        t2_sum = tf.nn.conv3d(
            t2, filters=self.filters, strides=self.strides, padding=self.padding
        )
        p2_sum = tf.nn.conv3d(
            p2, filters=self.filters, strides=self.strides, padding=self.padding
        )
        tp_sum = tf.nn.conv3d(
            tp, filters=self.filters, strides=self.strides, padding=self.padding
        )

        # average over kernel
        # (batch, dim1, dim2, dim3, 1)
        t_avg = t_sum / self.kernel_vol  # E[t]
        p_avg = p_sum / self.kernel_vol  # E[p]

        # shape = (batch, dim1, dim2, dim3, 1)
        cross = tp_sum - p_avg * t_sum  # E[tp] * E[1] - E[p] * E[t] * E[1]
        t_var = t2_sum - t_avg * t_sum  # V[t] * E[1]
        p_var = p2_sum - p_avg * p_sum  # V[p] * E[1]

        # (E[tp] - E[p] * E[t]) ** 2 / V[t] / V[p]
        num = cross * cross + EPS
        denom = t_var * p_var + EPS
        ncc = num / denom

        ncc = tf.debugging.check_numerics(
            ncc, "LNCC ncc value NAN/INF", name="LNCC_before_mean"
        )

        return tf.reduce_mean(ncc, axis=[1, 2, 3, 4])

    def get_config(self) -> dict:
        """Return the config dictionary for recreating this class."""
        config = super().get_config()
        config["kernel_size"] = self.kernel_size
        config["kernel_type"] = self.kernel_type
        return config


class LocalNormalizedCrossCorrelationDEBUG1(tf.keras.layers.Layer):
    """
    Local squared zero-normalized cross-correlation.

    Denote y_true as t and y_pred as p. Consider a window having n elements.
    Each position in the window corresponds a weight w_i for i=1:n.

    Define the discrete expectation in the window E[t] as

        E[t] = sum_i(w_i * t_i) / sum_i(w_i)

    Similarly, the discrete variance in the window V[t] is

        V[t] = E[t**2] - E[t] ** 2

    The local squared zero-normalized cross-correlation is therefore

        E[ (t-E[t]) * (p-E[p]) ] ** 2 / V[t] / V[p]

    where the expectation in numerator is

        E[ (t-E[t]) * (p-E[p]) ] = E[t * p] - E[t] * E[p]

    Different kernel corresponds to different weights.

    For now, y_true and y_pred have to be at least 4d tensor, including batch axis.

    Reference:

        - Zero-normalized cross-correlation (ZNCC):
            https://en.wikipedia.org/wiki/Cross-correlation
        - Code: https://github.com/voxelmorph/voxelmorph/blob/legacy/src/losses.py
    """

    kernel_fn_dict = dict(
        gaussian=gaussian_kernel1d,
        rectangular=rectangular_kernel1d,
        triangular=triangular_kernel1d,
    )

    def __init__(
        self,
        kernel_size: int = 9,
        kernel_type: str = "rectangular",
        # reduction: str = tf.keras.losses.Reduction.SUM,
        name: str = "LocalNormalizedCrossCorrelationDEBUG1",
    ):
        """
        Init.

        :param kernel_size: int. Kernel size or kernel sigma for kernel_type='gauss'.
        :param kernel_type: str, rectangular, triangular or gaussian
        :param name: name of the loss
        """
        super().__init__(name=name)
        if kernel_type not in self.kernel_fn_dict.keys():
            raise ValueError(
                f"Wrong kernel_type {kernel_type} for LNCC loss type. "
                f"Feasible values are {self.kernel_fn_dict.keys()}"
            )
        self.kernel_fn = self.kernel_fn_dict[kernel_type]
        self.kernel_type = kernel_type
        self.filters = tf.ones(
            shape=[self.kernel_size, self.kernel_size, self.kernel_size, 1, 1]
        )
        self.strides = [1, 1, 1, 1, 1]
        self.padding = "SAME"

        # E[1] = sum_i(w_i), ()
        self.kernel_vol = kernel_size ** 3

    def call(self, y_true: tf.Tensor, y_pred: tf.Tensor) -> tf.Tensor:
        """
        Return loss for a batch.

        :param y_true: shape = (batch, dim1, dim2, dim3)
            or (batch, dim1, dim2, dim3, ch)
        :param y_pred: shape = (batch, dim1, dim2, dim3)
            or (batch, dim1, dim2, dim3, ch)
        :return: shape = (batch,)
        """
        # adjust
        if len(y_true.shape) == 4:
            y_true = tf.expand_dims(y_true, axis=4)
            y_pred = tf.expand_dims(y_pred, axis=4)
        assert len(y_true.shape) == len(y_pred.shape) == 5

        # t = y_true, p = y_pred
        # (batch, dim1, dim2, dim3, ch)
        t2 = y_true * y_true
        p2 = y_pred * y_pred
        tp = y_true * y_pred

        # sum over kernel
        # (batch, dim1, dim2, dim3, 1)
        t_sum = tf.nn.conv3d(
            y_true, filters=self.filters, strides=self.strides, padding=self.padding
        )
        p_sum = tf.nn.conv3d(
            y_pred, filters=self.filters, strides=self.strides, padding=self.padding
        )
        t2_sum = tf.nn.conv3d(
            t2, filters=self.filters, strides=self.strides, padding=self.padding
        )
        p2_sum = tf.nn.conv3d(
            p2, filters=self.filters, strides=self.strides, padding=self.padding
        )
        tp_sum = tf.nn.conv3d(
            tp, filters=self.filters, strides=self.strides, padding=self.padding
        )

        # average over kernel
        # (batch, dim1, dim2, dim3, 1)
        t_avg = t_sum / self.kernel_vol  # E[t]
        p_avg = p_sum / self.kernel_vol  # E[p]

        # shape = (batch, dim1, dim2, dim3, 1)
        cross = tp_sum - p_avg * t_sum  # E[tp] * E[1] - E[p] * E[t] * E[1]
        t_var = t2_sum - t_avg * t_sum  # V[t] * E[1]
        p_var = p2_sum - p_avg * p_sum  # V[p] * E[1]

        # (E[tp] - E[p] * E[t]) ** 2 / V[t] / V[p]
        num = cross * cross + EPS
        denom = t_var * p_var + EPS

        denom = tf.debugging.check_numerics(
            denom, "DEBUG LNCC denom value NAN/INF", name="DEBUGLNCC_before_mean_denom"
        )

        return num

    def get_config(self) -> dict:
        """Return the config dictionary for recreating this class."""
        config = super().get_config()
        config["kernel_size"] = self.kernel_size
        config["kernel_type"] = self.kernel_type
        return config


class LocalNormalizedCrossCorrelationDEBUG2(tf.keras.layers.Layer):
    """
    Local squared zero-normalized cross-correlation.

    Denote y_true as t and y_pred as p. Consider a window having n elements.
    Each position in the window corresponds a weight w_i for i=1:n.

    Define the discrete expectation in the window E[t] as

        E[t] = sum_i(w_i * t_i) / sum_i(w_i)

    Similarly, the discrete variance in the window V[t] is

        V[t] = E[t**2] - E[t] ** 2

    The local squared zero-normalized cross-correlation is therefore

        E[ (t-E[t]) * (p-E[p]) ] ** 2 / V[t] / V[p]

    where the expectation in numerator is

        E[ (t-E[t]) * (p-E[p]) ] = E[t * p] - E[t] * E[p]

    Different kernel corresponds to different weights.

    For now, y_true and y_pred have to be at least 4d tensor, including batch axis.

    Reference:

        - Zero-normalized cross-correlation (ZNCC):
            https://en.wikipedia.org/wiki/Cross-correlation
        - Code: https://github.com/voxelmorph/voxelmorph/blob/legacy/src/losses.py
    """

    kernel_fn_dict = dict(
        gaussian=gaussian_kernel1d,
        rectangular=rectangular_kernel1d,
        triangular=triangular_kernel1d,
    )

    def __init__(
        self,
        kernel_size: int = 9,
        kernel_type: str = "rectangular",
        # reduction: str = tf.keras.losses.Reduction.SUM,
        name: str = "LocalNormalizedCrossCorrelationDEBUG2",
    ):
        """
        Init.

        :param kernel_size: int. Kernel size or kernel sigma for kernel_type='gauss'.
        :param kernel_type: str, rectangular, triangular or gaussian
        :param name: name of the loss
        """
        super().__init__(name=name)
        if kernel_type not in self.kernel_fn_dict.keys():
            raise ValueError(
                f"Wrong kernel_type {kernel_type} for LNCC loss type. "
                f"Feasible values are {self.kernel_fn_dict.keys()}"
            )
        self.kernel_fn = self.kernel_fn_dict[kernel_type]
        self.kernel_type = kernel_type
        self.filters = tf.ones(
            shape=[self.kernel_size, self.kernel_size, self.kernel_size, 1, 1]
        )
        self.strides = [1, 1, 1, 1, 1]
        self.padding = "SAME"

        # E[1] = sum_i(w_i), ()
        self.kernel_vol = kernel_size ** 3

    def call(self, y_true: tf.Tensor, y_pred: tf.Tensor) -> tf.Tensor:
        """
        Return loss for a batch.

        :param y_true: shape = (batch, dim1, dim2, dim3)
            or (batch, dim1, dim2, dim3, ch)
        :param y_pred: shape = (batch, dim1, dim2, dim3)
            or (batch, dim1, dim2, dim3, ch)
        :return: shape = (batch,)
        """
        # adjust
        if len(y_true.shape) == 4:
            y_true = tf.expand_dims(y_true, axis=4)
            y_pred = tf.expand_dims(y_pred, axis=4)
        assert len(y_true.shape) == len(y_pred.shape) == 5

        # t = y_true, p = y_pred
        # (batch, dim1, dim2, dim3, ch)
        t2 = y_true * y_true
        p2 = y_pred * y_pred
        tp = y_true * y_pred

        # sum over kernel
        # (batch, dim1, dim2, dim3, 1)
        t_sum = tf.nn.conv3d(
            y_true, filters=self.filters, strides=self.strides, padding=self.padding
        )
        p_sum = tf.nn.conv3d(
            y_pred, filters=self.filters, strides=self.strides, padding=self.padding
        )
        t2_sum = tf.nn.conv3d(
            t2, filters=self.filters, strides=self.strides, padding=self.padding
        )
        p2_sum = tf.nn.conv3d(
            p2, filters=self.filters, strides=self.strides, padding=self.padding
        )
        tp_sum = tf.nn.conv3d(
            tp, filters=self.filters, strides=self.strides, padding=self.padding
        )

        # average over kernel
        # (batch, dim1, dim2, dim3, 1)
        t_avg = t_sum / self.kernel_vol  # E[t]
        p_avg = p_sum / self.kernel_vol  # E[p]

        # shape = (batch, dim1, dim2, dim3, 1)
        cross = tp_sum - p_avg * t_sum  # E[tp] * E[1] - E[p] * E[t] * E[1]
        t_var = t2_sum - t_avg * t_sum  # V[t] * E[1]
        p_var = p2_sum - p_avg * p_sum  # V[p] * E[1]

        # (E[tp] - E[p] * E[t]) ** 2 / V[t] / V[p]
        num = cross * cross + EPS
        denom = t_var * p_var + EPS

        num = tf.debugging.check_numerics(
            num, "DEBUG LNCC num value NAN/INF", name="DEBUGLNCC_before_mean_num"
        )

        return denom

    def get_config(self) -> dict:
        """Return the config dictionary for recreating this class."""
        config = super().get_config()
        config["kernel_size"] = self.kernel_size
        config["kernel_type"] = self.kernel_type
        return config


@REGISTRY.register_loss(name="lncc")
class LocalNormalizedCrossCorrelationLoss(
    NegativeLossMixin, LocalNormalizedCrossCorrelation
):
    """Revert the sign of LocalNormalizedCrossCorrelation."""


class GlobalNormalizedCrossCorrelation(tf.keras.losses.Loss):
    """
    Global squared zero-normalized cross-correlation.

    Compute the squared cross-correlation between the reference and moving images
    y_true and y_pred have to be at least 4d tensor, including batch axis.

    Reference:

        - Zero-normalized cross-correlation (ZNCC):
            https://en.wikipedia.org/wiki/Cross-correlation

    """

    def __init__(
        self,
        reduction: str = tf.keras.losses.Reduction.AUTO,
        name: str = "GlobalNormalizedCrossCorrelation",
    ):
        """
        Init.
        :param reduction: using AUTO reduction,
            calling the loss like `loss(y_true, y_pred)` will return a scalar tensor.
        :param name: name of the loss
        """
        super().__init__(reduction=reduction, name=name)

    def call(self, y_true: tf.Tensor, y_pred: tf.Tensor) -> tf.Tensor:
        """
        Return loss for a batch.

        :param y_true: shape = (batch, ...)
        :param y_pred: shape = (batch, ...)
        :return: shape = (batch,)
        """

        axis = [a for a in range(1, len(y_true.shape))]
        mu_pred = tf.reduce_mean(y_pred, axis=axis, keepdims=True)
        mu_true = tf.reduce_mean(y_true, axis=axis, keepdims=True)
        var_pred = tf.math.reduce_variance(y_pred, axis=axis)
        var_true = tf.math.reduce_variance(y_true, axis=axis)
        numerator = tf.abs(
            tf.reduce_mean((y_pred - mu_pred) * (y_true - mu_true), axis=axis)
        )

        return (numerator * numerator + EPS) / (var_pred * var_true + EPS)


@REGISTRY.register_loss(name="gncc")
class GlobalNormalizedCrossCorrelationLoss(
    NegativeLossMixin, GlobalNormalizedCrossCorrelation
):
    """Revert the sign of GlobalNormalizedCrossCorrelation."""
