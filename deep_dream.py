"""Deep Dreaming using Caffe and Google's Inception convolutional neural network."""

# pylint: disable=invalid-name

import caffe
import numpy as np
from PIL import Image
from tqdm import tqdm


def to_image(arr):
    """Clips the values in a float32 image to 0-255 and converts it to a PIL image."""
    return Image.fromarray(np.uint8(np.clip(np.round(arr), 0, 255)))


def _resize(arr, size, method=Image.BICUBIC):
    h, w = size
    arr = np.float32(arr)
    if arr.ndim == 3:
        planes = [arr[i, :, :] for i in range(arr.shape[0])]
    else:
        raise TypeError('Only 3D CxHxW arrays are supported')
    imgs = [Image.fromarray(plane) for plane in planes]
    imgs_resized = [img.resize((w, h), method) for img in imgs]
    return np.stack([np.array(img) for img in imgs_resized])


class _LayerIndexer:
    def __init__(self, net, attr):
        self.net, self.attr = net, attr

    def __getitem__(self, key):
        return getattr(self.net.blobs[key], self.attr)[0]

    def __setitem__(self, key, value):
        getattr(self.net.blobs[key], self.attr)[0] = value


class CNN:
    """Represents an instance of a Caffe convolutional neural network."""

    def __init__(self, gpu=None):
        """Initializes a CNN.

        Args:
            gpu (Optional[int]): If present, Caffe will use this GPU device number. On a typical
                system with one GPU, it should be 0. If not present Caffe will use the CPU.
        """
        self.start = 'data'
        self.net = caffe.Classifier('bvlc_googlenet/deploy.prototxt',
                                    'bvlc_googlenet/bvlc_googlenet.caffemodel',
                                    mean=np.float32((103.939, 116.779, 123.68)),
                                    channel_swap=(2, 1, 0))
        self.data = _LayerIndexer(self.net, 'data')
        self.diff = _LayerIndexer(self.net, 'diff')
        self.img = np.zeros_like(self.data[self.start])
        self.total_px = 0
        self.progress_bar = None
        if gpu is not None:
            caffe.set_device(gpu)
            caffe.set_mode_gpu()
        else:
            caffe.set_mode_cpu()

    def _preprocess(self, img):
        return np.rollaxis(np.float32(img), 2)[::-1] - self.net.transformer.mean['data']

    def _deprocess(self, img):
        return np.dstack((img + self.net.transformer.mean['data'])[::-1])

    def _grad_tiled(self, end, progress=False, max_tile_size=512):
        if progress:
            if not self.progress_bar:
                self.progress_bar = tqdm(
                    total=self.total_px, unit='pix', unit_scale=True, ncols=80, smoothing=0)

        h, w = self.img.shape[1:]  # Height and width of input image
        ny, nx = (h-1)//max_tile_size+1, (w-1)//max_tile_size+1  # Number of tiles per dimension
        g = np.zeros_like(self.img)
        for y in range(ny):
            th = h//ny
            if y == ny-1:
                th += h - th*ny
            for x in range(nx):
                tw = w//nx
                if x == nx-1:
                    tw += w - tw*nx
                self.net.blobs[self.start].reshape(1, 3, th, tw)
                sy, sx = h//ny*y, w//nx*x
                self.data[self.start] = self.img[:, sy:sy+th, sx:sx+tw]
                self.net.forward(end=end)
                self.diff[end] = self.data[end]
                self.net.backward(start=end)
                g[:, sy:sy+th, sx:sx+tw] = self.diff[self.start]

                if progress:
                    self.progress_bar.update(th*tw)
        return g

    def _step(self, n=1, step_size=1.5, jitter=32, **kwargs):
        for _ in range(n):
            x, y = np.random.randint(-jitter, jitter+1, 2)
            self.img = np.roll(np.roll(self.img, x, 2), y, 1)
            g = self._grad_tiled(**kwargs)
            self.img += step_size * g / np.median(np.abs(g))
            self.img = np.roll(np.roll(self.img, -x, 2), -y, 1)

    def _octave_detail(self, base, scale=4, n=10, per_octave=2, **kwargs):
        factor = 2**(1/per_octave)
        detail = np.zeros_like(base, dtype=np.float32)
        self.total_px += base.shape[1] * base.shape[2] * n
        if scale != 1:
            hf, wf = np.int32(np.ceil(np.array(base.shape)[-2:]/factor))
            smaller_base = _resize(base, (hf, wf))
            smaller_detail = self._octave_detail(smaller_base, scale-1, n, per_octave, **kwargs)
            detail = _resize(smaller_detail, base.shape[-2:])
        self.img = base + detail
        self._step(n, **kwargs)
        return self.img - base

    def layers(self):
        """Returns a list of layer names, suitable for the 'end' argument of dream()."""
        return self.net.layers()

    def dream(self, input_img, end, progress=True, **kwargs):
        """Runs the Deep Dream multiscale gradient ascent algorithm on the input image.

        Args:
            input_img: The image to process (PIL images or Numpy arrays are accepted)
            end (str): The layer to use as an objective function for gradient ascent.
            progress (Optional[bool]): Display a progress bar while computing.
            scale (Optional[int]): The number of scales to process.
            per_octave (Optional[int]): Determines the difference between each scale; for instance,
                the default of 2 means that a 1000x1000 input image will get processed as 707x707
                and 500x500.
            n (Optional[int]): The number of gradient ascent steps per scale. Defaults to 10.
            step_size (Optional[float]): The strength of each individual gradient ascent step.
                Specifically, each step will change the image's pixel values by a median of
                step_size.
            max_tile_size (Optional[int]): Defaults to 512, suitable for a GPU with 2 GB RAM.
                Higher values perform better; if Caffe runs out of GPU memory and crashes then it
                should be lowered.

        Returns:
            The processed image, as a PIL image.
        """
        for blob in self.net.blobs:
            self.diff[blob] = 0
        input_arr = self._preprocess(np.float32(input_img))
        np.random.seed(0)
        self.total_px = 0
        self.progress_bar = None
        try:
            detail = self._octave_detail(input_arr, end=end, progress=progress, **kwargs)
        finally:
            if self.progress_bar:
                self.progress_bar.close()
        return to_image(self._deprocess(detail + input_arr))
