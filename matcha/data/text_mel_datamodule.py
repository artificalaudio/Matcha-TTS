import random
from typing import Any, Dict, Optional

import torch
import torchaudio as ta
from lightning import LightningDataModule
from torch.utils.data.dataloader import DataLoader
import onnxruntime
import numpy as np


from onnxruntime import set_seed

from matcha.text import text_to_sequence
from matcha.utils.audio import mel_spectrogram
from matcha.utils.model import fix_len_compatibility, normalize
from matcha.utils.utils import intersperse

# adapted from : https://github.com/RVC-Project/Retrieval-based-Voice-Conversion
# add license, credit
# this also assumes you want the final projection layer, which according to ghenter (add reference to the two papers on SSL sponteaounous speech), you would want to use other intermediate feature layers. 
class SSLFeatureExtractor:
    def __init__(self, vec_path="/content/drive/MyDrive/Models/placeWhereYourFeatureExtractorLives.onnx", device=None):
        print("load model(s) from {}".format(vec_path))
        if device == "cpu" or device is None:
            providers = ["CPUExecutionProvider"]
        else:
            raise RuntimeError("Unsportted Device")
        self.model = onnxruntime.InferenceSession(vec_path, providers=providers)

    def __call__(self, wav):
        return self.forward(wav)

    def forward(self, wav):
        feats = wav
        print(feats.shape)
        if feats.ndim == 2:  # double channels
            feats = feats.mean(-1)
        assert feats.ndim == 1, feats.ndim
        feats = np.expand_dims(np.expand_dims(feats, 0), 0)
        print(feats.shape)
        onnx_input = {self.model.get_inputs()[0].name: feats}
        logits = self.model.run(None, onnx_input)[0]
        return logits.transpose(0, 2, 1)

class RMVPEOnnxPitchExtractor:

    def __init__(self, file: str, threshold: float = 0.03):
        self.file = file
        self.f0_min = 50
        self.f0_max = 1100
        self.f0_mel_min = 1127 * np.log(1 + self.f0_min / 700)
        self.f0_mel_max = 1127 * np.log(1 + self.f0_max / 700)
        self.threshold = threshold
        self.model = onnxruntime.InferenceSession(file, providers=["CPUExecutionProvider"])

    def extract(self, audio, pitchf, f0_up_key, sr, window, silence_front=0):
        try:
            # Data conversion
            if not isinstance(audio, np.ndarray):
                audio = audio.cpu().numpy()

            if not isinstance(pitchf, np.ndarray):
                pitchf = pitchf.cpu().numpy().astype(np.float32)

            if audio.ndim != 1:
                raise RuntimeError(f"Exception in {self.__class__.__name__}: audio.ndim is not 1 (size: {audio.ndim}, shape: {audio.shape})")

            if pitchf.ndim != 1:
                raise RuntimeError(f"Exception in {self.__class__.__name__}: pitchf.ndim is not 1 (size: {pitchf.ndim}, shape: {pitchf.shape})")


            # silenceFrontFrame = silence_front * sr
            # startWindow = int(silenceFrontFrame / window)
            # silenceFrontFrameOffset = startWindow * window
            # targetFrameLength = len(audio) - silenceFrontFrameOffset
            minimumFrames = 0.01 * sr
            # targetFrameLength = max(minimumFrames, targetFrameLength)
            # print(targetFrameLength)
            targetFrameLength = int(minimumFrames)
            # print(audio.size)
            audio = audio[targetFrameLength:]
            audio = np.expand_dims(audio, axis=0)
            # print(audio.size)

            output = self.model.run(
                ["f0","uv"],
                {
                    "waveform": audio.astype(np.float32),
                    "threshold": np.array([self.threshold]).astype(np.float32),
                }
            )

            # print("out size", len(output))

            f0 = output[0].squeeze()
            f0 *= pow(2, f0_up_key / 12)
            pitchf[-f0.shape[0]:] = f0[:pitchf.shape[0]]

            f0_mel = 1127.0 * np.log(1.0 + pitchf / 700.0)
            f0_mel[f0_mel > 0] = (f0_mel[f0_mel > 0] - self.f0_mel_min) * 254 / (self.f0_mel_max - self.f0_mel_min) + 1
            f0_mel[f0_mel <= 1] = 1
            f0_mel[f0_mel > 255] = 255
            f0_coarse = np.rint(f0_mel).astype(int)

        except Exception as e:
            raise RuntimeError(f"Exception in {self.__class__.__name__}", e)

        return f0_coarse, pitchf


def parse_filelist(filelist_path, split_char="|"):
    with open(filelist_path, encoding="utf-8") as f:
        filepaths_and_text = [line.strip().split(split_char) for line in f]
    return filepaths_and_text


class TextMelDataModule(LightningDataModule):
    def __init__(  # pylint: disable=unused-argument
        self,
        name,
        train_filelist_path,
        valid_filelist_path,
        batch_size,
        num_workers,
        pin_memory,
        cleaners,
        add_blank,
        n_spks,
        n_fft,
        n_feats,
        sample_rate,
        hop_length,
        win_length,
        f_min,
        f_max,
        data_statistics,
        seed,
    ):
        super().__init__()

        # this line allows to access init params with 'self.hparams' attribute
        # also ensures init params will be stored in ckpt
        self.save_hyperparameters(logger=False)

    def setup(self, stage: Optional[str] = None):  # pylint: disable=unused-argument
        """Load data. Set variables: `self.data_train`, `self.data_val`, `self.data_test`.

        This method is called by lightning with both `trainer.fit()` and `trainer.test()`, so be
        careful not to execute things like random split twice!
        """
        # load and split datasets only if not loaded already

        self.trainset = TextMelDataset(  # pylint: disable=attribute-defined-outside-init
            self.hparams.train_filelist_path,
            self.hparams.n_spks,
            self.hparams.cleaners,
            self.hparams.add_blank,
            self.hparams.n_fft,
            self.hparams.n_feats,
            self.hparams.sample_rate,
            self.hparams.hop_length,
            self.hparams.win_length,
            self.hparams.f_min,
            self.hparams.f_max,
            self.hparams.data_statistics,
            self.hparams.seed,
        )
        self.validset = TextMelDataset(  # pylint: disable=attribute-defined-outside-init
            self.hparams.valid_filelist_path,
            self.hparams.n_spks,
            self.hparams.cleaners,
            self.hparams.add_blank,
            self.hparams.n_fft,
            self.hparams.n_feats,
            self.hparams.sample_rate,
            self.hparams.hop_length,
            self.hparams.win_length,
            self.hparams.f_min,
            self.hparams.f_max,
            self.hparams.data_statistics,
            self.hparams.seed,
        )

    def train_dataloader(self):
        return DataLoader(
            dataset=self.trainset,
            batch_size=self.hparams.batch_size,
            num_workers=self.hparams.num_workers,
            pin_memory=self.hparams.pin_memory,
            shuffle=True,
            collate_fn=TextMelBatchCollate(self.hparams.n_spks),
        )

    def val_dataloader(self):
        return DataLoader(
            dataset=self.validset,
            batch_size=self.hparams.batch_size,
            num_workers=self.hparams.num_workers,
            pin_memory=self.hparams.pin_memory,
            shuffle=False,
            collate_fn=TextMelBatchCollate(self.hparams.n_spks),
        )

    def teardown(self, stage: Optional[str] = None):
        """Clean up after fit or test."""
        pass  # pylint: disable=unnecessary-pass

    def state_dict(self):  # pylint: disable=no-self-use
        """Extra things to save to checkpoint."""
        return {}

    def load_state_dict(self, state_dict: Dict[str, Any]):
        """Things to do when loading checkpoint."""
        pass  # pylint: disable=unnecessary-pass


class TextMelDataset(torch.utils.data.Dataset):
    def __init__(
        self,
        filelist_path,
        n_spks,
        cleaners,
        add_blank=True,
        n_fft=1024,
        n_mels=80,
        sample_rate=22050,
        hop_length=256,
        win_length=1024,
        f_min=0.0,
        f_max=8000,
        data_parameters=None,
        seed=None,
    ):
        self.filepaths_and_text = parse_filelist(filelist_path)
        self.n_spks = n_spks
        self.cleaners = cleaners
        self.add_blank = add_blank
        self.n_fft = n_fft
        self.n_mels = n_mels
        self.sample_rate = sample_rate
        self.hop_length = hop_length
        self.win_length = win_length
        self.f_min = f_min
        self.f_max = f_max
        if data_parameters is not None:
            self.data_parameters = data_parameters
        else:
            self.data_parameters = {"mel_mean": 0, "mel_std": 1}
        random.seed(seed)
        random.shuffle(self.filepaths_and_text)

    def get_datapoint(self, filepath_and_text):
        if self.n_spks > 1:
            filepath, spk, text = (
                filepath_and_text[0],
                int(filepath_and_text[1]),
                filepath_and_text[2],
            )
        else:
            filepath, text = filepath_and_text[0], filepath_and_text[1]
            spk = None

        text = self.get_text(text, add_blank=self.add_blank)
        # if mel OG method do this:
        mel = self.get_mel(filepath)
        #if SSL method do this:
        # mel = self.get_ssl(filepath) # kinda lazy, just overridding mel and not being descriptive. Could change var Mel for reps (representations?)
        # if Encodec method, do this etc...
        # reps = encodec.decode balah blah
        return {"x": text, "y": mel, "spk": spk}

    def get_mel(self, filepath):
        audio, sr = ta.load(filepath)
        assert sr == self.sample_rate
        mel = mel_spectrogram(
            audio,
            self.n_fft,
            self.n_mels,
            self.sample_rate,
            self.hop_length,
            self.win_length,
            self.f_min,
            self.f_max,
            center=False,
        ).squeeze()
        mel = normalize(mel, self.data_parameters["mel_mean"], self.data_parameters["mel_std"])
        return mel

    def get_ssl(self, filepath):
        audio, sr = ta.load(filepath)
        assert sr == self.sample_rate
        ssl_model = SSLFeatureExtractor("./vecpath.onnx")
        # Resample to 16khz, feed through model, return vec of (Frames X 256), where frame is 10ms.
        # these are stand in functions for now, just trying to get a feel for what needs to happen/change.
        ssl = ssl_model(audio
        )
        # shape will be (1, N, 256), this needs converting/squeeze the 1, rearrange to format Matcha wants, N would be frames over time,
        # 256 would be depth, this could be generalised further? Like either that number inferred from depth of network, or macro param?
        # ssl = normalize(mel, self.data_parameters["mel_mean"], self.data_parameters["mel_std"])
        # Would we still normalise if we're using embeddings? 
        return ssl

    def get_ssl_F0(self, filepath):
        audio, sr = ta.load(filepath)
        assert sr == self.sample_rate
        ssl_model = SSLFeatureExtractor("./vecpath.onnx")
        # Resample to 16khz, feed through model, return vec of (Frames X 256), where frame is 10ms.
        ssl = ssl_model(audio
        ).squeeze() #?? Do we just squeeze? I'd rather make sure array was orientated correctly before squeezing.
        #ssl = normalize(mel, self.data_parameters["mel_mean"], self.data_parameters["mel_std"])
        # Do similar thing, load RMPVE neural pitch tracker via onnx, extract, filter out extra frames, align to same time len
        F0 = F0featureExtractor(audio)
        return ssl, F0

    def get_text(self, text, add_blank=True):
        text_norm = text_to_sequence(text, self.cleaners)
        if self.add_blank:
            text_norm = intersperse(text_norm, 0)
        text_norm = torch.IntTensor(text_norm)
        return text_norm

    def __getitem__(self, index):
        datapoint = self.get_datapoint(self.filepaths_and_text[index])
        return datapoint

    def __len__(self):
        return len(self.filepaths_and_text)


class TextMelBatchCollate:
    def __init__(self, n_spks):
        self.n_spks = n_spks

    def __call__(self, batch):
        B = len(batch)
        y_max_length = max([item["y"].shape[-1] for item in batch])
        y_max_length = fix_len_compatibility(y_max_length)
        x_max_length = max([item["x"].shape[-1] for item in batch])
        n_feats = batch[0]["y"].shape[-2]

        y = torch.zeros((B, n_feats, y_max_length), dtype=torch.float32)
        x = torch.zeros((B, x_max_length), dtype=torch.long)
        y_lengths, x_lengths = [], []
        spks = []
        for i, item in enumerate(batch):
            y_, x_ = item["y"], item["x"]
            y_lengths.append(y_.shape[-1])
            x_lengths.append(x_.shape[-1])
            y[i, :, : y_.shape[-1]] = y_
            x[i, : x_.shape[-1]] = x_
            spks.append(item["spk"])

        y_lengths = torch.tensor(y_lengths, dtype=torch.long)
        x_lengths = torch.tensor(x_lengths, dtype=torch.long)
        spks = torch.tensor(spks, dtype=torch.long) if self.n_spks > 1 else None

        return {"x": x, "x_lengths": x_lengths, "y": y, "y_lengths": y_lengths, "spks": spks}
