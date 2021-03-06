import os
import random
from pathlib import Path
from typing import Callable, Dict, List, Tuple, Union

import pytorch_lightning as pl
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torchvision.models as models
import torchvision.transforms as transforms
from PIL import Image
from pytorch_lightning import LightningModule
from pytorch_lightning import loggers as pl_loggers
from pytorch_lightning.callbacks import Callback, ModelCheckpoint
from pytorch_lightning.callbacks import ModelCheckpoint
from pytorch_lightning.callbacks import LearningRateMonitor
from torch import Tensor
from torch.nn import BCEWithLogitsLoss
from torch.utils.data import DataLoader, Dataset, SubsetRandomSampler


# NOTE: code below written for use in Python / Cluster
# We install:
#   * Pytorch Lightning Version 1.2.1 (https://github.com/PyTorchLightning/pytorch-lightning)
#   * torch Version 1.7.1 (https://github.com/pytorch/pytorch)
#   * torchvision Version 0.8.2 (https://github.com/pytorch/vision)
#   * pathlib Version 1.0.1 (https://pathlib.readthedocs.io/en/pep428/)
#   * pillow Version 7.0.0 (https://github.com/python-pillow/Pillow/blob/88bd672dafad68b419ea29bef941dfa17f941038/docs/installation.rst)

HOME = os.environ.get("HOME")
ROOT = Path(str(HOME) + "/projects/def-jlevman/x2019/covid")
BASE = ROOT / "networks" / "squeezenet"
IMG_DIR = ROOT / "dataset" / "images"


COVID_TEXT_LABELS = Path(str(ROOT) + "/dataset/covid_text_labels")
COVID_TRAIN = COVID_TEXT_LABELS / "covid_train_labels.txt"
COVID_VAL = COVID_TEXT_LABELS / "covid_val_labels.txt"
COVID_TEST = COVID_TEXT_LABELS / "covid_test_labels.txt"

NONCOVID_TEXT_LABELS = Path(str(ROOT) + "/dataset/noncovid_text_labels")
NONCOVID_TRAIN = NONCOVID_TEXT_LABELS / "noncovid_train_labels.txt"
NONCOVID_VAL = NONCOVID_TEXT_LABELS / "noncovid_val_labels.txt"
NONCOVID_TEST = NONCOVID_TEXT_LABELS / "noncovid_test_labels.txt"

DICT_PATH = BASE / "squeezenet1_1.pth"

BATCH_SIZE = 8

# Models participating in Bagging, model directores are stored in this list
# for example, "3" here means that only one model which is in model directory "3" 
# is selected with regards to best validation accuracy

MODEL_LIST = [3]

INPUT_SIZE = (224, 224)


def read_text_labels(text_path: str) -> List[str]:
    """read file in `txt_path`, stripping away leading and trailing whitespace"""
    with open(text_path) as f:
        lines = f.readlines()
    txt_data = [line.strip() for line in lines]
    return txt_data

class CovidDataset(Dataset):
    def __init__(self, root_dir, text_COVID, text_NonCOVID, transform):
        """
        Args:
            txt_path (string): Path to the txt file with annotations.
            root_dir (string): Directory with all the images.
        """
        self.root_dir = root_dir
        self.text_path = [text_COVID, text_NonCOVID]
        self.classes = ["CT_COVID", "CT_NonCOVID"]
        self.covid_images = read_text_labels(text_COVID)
        self.noncovid_images = read_text_labels(text_NonCOVID)
        self.covid_path = Path(root_dir).resolve() / "CT_COVID"
        self.noncovid_path = Path(root_dir).resolve() / "CT_NonCOVID"
        self.covid_images = [self.covid_path /
                             img for img in self.covid_images]
        self.noncovid_images = [self.noncovid_path /
                                img for img in self.noncovid_images]
        self.images = self.covid_images + self.noncovid_images
        self.transform = transform

    def __len__(self):
        return len(self.images)

    def __getitem__(self, index):

        if torch.is_tensor(index):
            index = index.tolist()

        img_path = self.images[index]
        parent = img_path.parent.name
        label = 1 if parent == "CT_COVID" else 0

        image = Image.open(img_path).convert("RGB")

        if self.transform:
            image = self.transform(image)

        return image, label


"""The helper function generates the transformed trainset, valset and testset."""
def covid_dataset_helper():
    def get_transforms(input_size: int = INPUT_SIZE):

        # The mean and std values is suggested by torchvision.model documentation.
        # link: https://pytorch.org/vision/0.8/models.html
        normalize = transforms.Normalize(
            mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])

        train_transform = transforms.Compose(
            [
                # The transform methods and parameters are adopted from 
                # https://github.com/UCSD-AI4H/COVID-CT/blob/master/baseline%20methods/Self-Trans/CT-predict-pretrain.ipynb
                transforms.Resize(256),
                transforms.RandomResizedCrop((224), scale=(0.5, 1.0)),
                transforms.RandomHorizontalFlip(),
                transforms.ColorJitter(brightness=0.2, contrast=0.2),
                transforms.ToTensor(),
                normalize,
            ]
        )

        val_transform = transforms.Compose(
            [
                transforms.Resize((224, 224)),
                transforms.ToTensor(),
                normalize,
            ]
        )
        return train_transform, val_transform

    train_transform, val_transform = get_transforms()
    trainset = CovidDataset(
        root_dir=str(ROOT),
        text_COVID=str(COVID_TRAIN),
        text_NonCOVID=str(NONCOVID_TRAIN),
        transform=train_transform,
    )
    valset = CovidDataset(
        root_dir=str(ROOT),
        text_COVID=str(COVID_VAL),
        text_NonCOVID=str(NONCOVID_VAL),
        transform=val_transform,
    )
    testset = CovidDataset(
        root_dir=str(ROOT),
        text_COVID=str(COVID_TEST),
        text_NonCOVID=str(NONCOVID_TEST),
        transform=val_transform,
    )

    return trainset, valset, testset


class SqueezenetTransferLearning(LightningModule):
    """
    Lightning Module is adapted from https://pytorch-lightning.readthedocs.io/en/stable/starter/introduction_guide.html
    """
    def __init__(self, input_shape):
        super().__init__()

        self.save_hyperparameters()
        
        self.input_shape = input_shape
        self.accuracy = pl.metrics.Accuracy()
        # Initializing a pretrained rsqueezenet1.1 model.
        self.feature_extractor = models.squeezenet1_1(pretrained=False)
        self.feature_extractor.load_state_dict(torch.load(DICT_PATH))
        self.feature_extractor.eval()
        self.classfier = nn.Linear(1000, 1)

    def forward(self, x):
        self.feature_extractor.eval()
        x = self.feature_extractor(x)
        x = x.unsqueeze(1)
        x = self.classfier(x)
        x = x.squeeze()
        return x

    def training_step(self, batch, batch_idx):
        # training_step defined the train loop. It is independent of forward
        x, y = batch
        output = self(x)
        criterion = BCEWithLogitsLoss()
        loss = criterion(output.unsqueeze(1), y.unsqueeze(1).float())  
        logits = torch.sigmoid(output)
        pred = torch.round(logits)
        acc = torch.mean((pred == y).float())
        self.log('train_acc_step', acc, prog_bar=True)
        self.log('train_loss', loss, on_step=True, on_epoch=True, logger=True)

        return loss

    def validation_step(self, batch, batch_idx):
        x, y = batch
        output = self(x)
        criterion = BCEWithLogitsLoss()
        logits = torch.sigmoid(output)
        pred = torch.round(logits)
        acc = torch.mean((pred == y).float())
        self.log('val_acc', acc, prog_bar=True)
        loss = criterion(output.unsqueeze(1), y.unsqueeze(1).float())
        self.log('val_loss', loss)

        return loss

    def test_step(self, batch, batch_idx):
        x, y = batch
        output = self(x)
        criterion = BCEWithLogitsLoss()
        logits = torch.sigmoid(output)
        pred = torch.round(logits)
        self.test_pred.append(pred)
        acc = torch.mean((pred == y).float())
        self.log('test_acc', acc, prog_bar=True)
        loss = criterion(output.unsqueeze(1), y.unsqueeze(1).float())
        self.log('test_loss', loss)

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), lr=1e-5)
        lr_scheduler = {
        'scheduler': torch.optim.lr_scheduler.StepLR(optimizer, step_size=150, 
        gamma=0.5),
        'name': 'learning rate',
         }
        return [optimizer], [lr_scheduler]


"""Generate dataloaders for bagging"""
def bagging_loader(dataset: CovidDataset, percent: float = 0.6, shuffle: bool = False) -> DataLoader:
    # Each bag contains 60% samples of the original dataset
    size = int(round(percent * len(dataset)))
    idx = random.sample(range(0, len(dataset)), size)

    return DataLoader(dataset, sampler=SubsetRandomSampler(idx), batch_size=BATCH_SIZE, drop_last=True, shuffle=shuffle)




def models_bagging():
    test_acc = 0


    # Using logits in major vote 
    pred_dic = {}
    model_list_logits = []

   
    # Load selected models
    for n in MODEL_LIST:
        model_path = BASE / "models" / str(n) / "last.ckpt"
        model = SqueezenetTransferLearning.load_from_checkpoint(model_path)
        model_list_logits.append(model)

    for model in model_list_logits:
        model.eval()
        model.freeze()

        labels_tensor = torch.tensor([])
        logits_tensor = torch.tensor([])

        for (input, labels) in test_loader:
            output = model(input)
            logits = torch.sigmoid(output)
            labels_tensor = torch.cat((labels_tensor, labels))
            logits_tensor = torch.cat((logits_tensor, logits))
            pred_dic[model_list_logits.index(model)] = logits_tensor


    value = pred_dic.values()
    total = sum(value)
    pred_list = torch.round((total/len(model_list_logits)))
    test_acc = torch.mean((pred_list==labels_tensor).float())
    
    print("test acc:", test_acc)





if __name__ == "__main__":
    _, _, testset = covid_dataset_helper() 
    test_loader = DataLoader(testset, batch_size=BATCH_SIZE, drop_last=True, shuffle=False)
    models_bagging()
















    
   
