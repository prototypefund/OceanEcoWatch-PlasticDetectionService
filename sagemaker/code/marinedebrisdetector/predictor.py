import io
from itertools import product

import numpy as np
import rasterio
import torch
from rasterio.io import MemoryFile
from rasterio.windows import Window
from scipy.ndimage.filters import gaussian_filter
from tqdm import tqdm

from marinedebrisdetector.model.unet import UNet
from marinedebrisdetector.transforms import get_transform


class ScenePredictor:
    def __init__(
        self,
        image_size=(480, 480),
        device="cpu",
        offset=64,  # todo patch size 480, 480, why add_fdi_ndvi?
        # Thought it was only used for training, what does offset mean?
        use_test_aug=2,
        add_fdi_ndvi=False,
        activation="sigmoid",
    ):
        self.image_size = image_size
        self.activation = activation
        self.device = device
        self.offset = offset  # remove border effects from the CNN
        self.use_test_aug = use_test_aug

        self.model = UNet(n_channels=12, n_classes=1, bilinear=False)
        self.transform = get_transform(
            "test", add_fdi_ndvi=add_fdi_ndvi, cropsize=image_size[0]
        )

    def predict(self, model, data: io.BytesIO) -> bytes:
        src = rasterio.open(data)
        meta = src.meta.copy()
        self.model = model.to(self.device)
        self.model.eval()

        meta["count"] = 1
        meta["dtype"] = "uint8"

        # Window(col_off, row_off, width, height)
        H, W = self.image_size

        rows = np.arange(0, meta["height"], H)
        cols = np.arange(0, meta["width"], W)

        image_window = Window(0, 0, meta["width"], meta["height"])
        with MemoryFile() as memfile:
            with memfile.open(**meta) as dst:
                for r, c in tqdm(
                    product(rows, cols), total=len(rows) * len(cols), leave=False
                ):
                    H, W = self.image_size

                    window = image_window.intersection(
                        Window(
                            c - self.offset,
                            r - self.offset,
                            W + self.offset,
                            H + self.offset,
                        )
                    )
                    image = src.read(window=window)

                    # read only 12 bands
                    if image.shape[0] == 13:
                        image = image[:12]

                    # pad with zeros
                    H, W = self.image_size
                    H, W = H + self.offset * 2, W + self.offset * 2

                    bands, h, w = image.shape
                    dh = (H - h) / 2
                    dw = (W - w) / 2
                    image = np.pad(
                        image,
                        [
                            (0, 0),
                            (int(np.ceil(dh)), int(np.floor(dh))),
                            (int(np.ceil(dw)), int(np.floor(dw))),
                        ],
                    )

                    # to torch + normalize
                    image = torch.from_numpy(image.astype(np.float32))
                    image = image.to(self.device) * 1e-4

                    # predict
                    with torch.no_grad():
                        x = image.unsqueeze(0)

                        out = self.model(x)

                        if isinstance(out, tuple):
                            y_score, y_pred = self.model(x)
                            y_score = y_score.squeeze().cpu().numpy()
                            y_pred = y_pred.squeeze().cpu().numpy()
                        else:
                            y_logits = out.squeeze(0)

                            if self.activation == "sigmoid":
                                y_logits = torch.sigmoid(y_logits)

                            y_score = y_logits.cpu().detach().numpy()[0]

                    # unpad
                    y_score = y_score[
                        int(np.ceil(dh)) : y_score.shape[0] - int(np.floor(dh)),
                        int(np.ceil(dw)) : y_score.shape[1] - int(np.floor(dw)),
                    ]
                    assert y_score.shape[0] == window.height, "unpadding size mismatch"
                    assert y_score.shape[1] == window.width, "unpadding size mismatch"

                    data = dst.read(window=window)[0] / 255
                    overlap = data > 0

                    if overlap.any():
                        # smooth transition in overlapping regions
                        dx, dy = np.gradient(overlap.astype(float))  # get border
                        g = np.abs(dx) + np.abs(dy)
                        transition = gaussian_filter(g, sigma=self.offset / 2)
                        transition /= transition.max()
                        transition[~overlap] = 1.0  # normalize to 1

                        y_score = transition * y_score + (1 - transition) * data

                    # write
                    writedata = (
                        np.expand_dims(y_score, 0).astype(np.float32) * 255
                    ).astype(np.uint8)
                    dst.write(writedata, window=window)
        src.close()
        memfile.seek(0)
        return memfile.read()
