import mxnet as mx
import random
from mxnet.gluon.data.vision import transforms
from functools import partial
from gluoncv.utils import LRScheduler
from easydict import EasyDict as edict
from albumentations import (
    Compose, ShiftScaleRotate, PadIfNeeded, RandomCrop,
    RGBShift, RandomBrightnessContrast, RandomRotate90, Flip
)

from isegm.engine.trainer import ISTrainer
from isegm.model.is_model import get_model
from isegm.model.losses import SigmoidBinaryCrossEntropyLoss
from isegm.model.metrics import AdaptiveIoU
from isegm.data.sbd import SBDDataset
from isegm.data.points_sampler import MultiPointSampler
from isegm.utils.log import logger


def main(cfg):
    model, model_cfg = init_model(cfg)
    train(model, cfg, model_cfg, start_epoch=cfg.start_epoch)


def init_model(cfg):
    model_cfg = edict()
    model_cfg.syncbn = True
    model_cfg.crop_size = (320, 480)
    model_cfg.input_normalization = {
        'mean': [.485, .456, .406],
        'std': [.229, .224, .225]
    }
    model_cfg.num_max_points = 10

    model_cfg.input_transform = transforms.Compose([
        transforms.ToTensor(),
        transforms.Normalize(model_cfg.input_normalization['mean'],
                             model_cfg.input_normalization['std']),
    ])

    if cfg.ngpus > 1 and model_cfg.syncbn:
        norm_layer = partial(mx.gluon.contrib.nn.SyncBatchNorm, num_devices=cfg.ngpus)
    else:
        norm_layer = mx.gluon.nn.BatchNorm

    model = get_model(norm_layer=norm_layer, backbone_norm_layer=None,
                      max_interactive_points=model_cfg.num_max_points,
                      backbone='resnet50',
                      deeplab_ch=128, aspp_dropout=0.20)
    model.initialize(mx.init.Xavier(rnd_type='gaussian', magnitude=2), ctx=mx.cpu(0))
    model.feature_extractor.load_pretrained_weights()

    return model, model_cfg


def train(model, cfg, model_cfg, start_epoch=0):
    cfg.batch_size = 28 if cfg.batch_size < 1 else cfg.batch_size
    cfg.val_batch_size = cfg.batch_size
    cfg.input_normalization = model_cfg.input_normalization
    crop_size = model_cfg.crop_size

    loss_cfg = edict()
    loss_cfg.instance_loss = SigmoidBinaryCrossEntropyLoss()
    loss_cfg.instance_loss_weight = 1.0

    num_epochs = 120
    num_masks = 1

    train_augmentator = Compose([
        Flip(),
        RandomRotate90(),
        ShiftScaleRotate(shift_limit=0.03, scale_limit=0,
                         rotate_limit=(-3, 3), border_mode=0, p=0.75),
        PadIfNeeded(min_height=crop_size[0], min_width=crop_size[1], border_mode=0),
        RandomCrop(*crop_size),
        RandomBrightnessContrast(brightness_limit=(-0.25, 0.25), contrast_limit=(-0.15, 0.4), p=0.75),
        RGBShift(r_shift_limit=10, g_shift_limit=10, b_shift_limit=10, p=0.75)
    ], p=1.0)

    val_augmentator = Compose([
        PadIfNeeded(min_height=crop_size[0], min_width=crop_size[1], border_mode=0),
        RandomCrop(*crop_size)
    ], p=1.0)

    def scale_func(image_shape):
        return random.uniform(0.75, 1.25)

    points_sampler = MultiPointSampler(model_cfg.num_max_points, prob_gamma=0.7,
                                       merge_objects_prob=0.15,
                                       max_num_merged_objects=2)

    trainset = SBDDataset(
        cfg.SBD_PATH,
        split='train',
        num_masks=num_masks,
        augmentator=train_augmentator,
        points_from_one_object=False,
        input_transform=model_cfg.input_transform,
        min_object_area=80,
        keep_background_prob=0.0,
        image_rescale=scale_func,
        points_sampler=points_sampler,
        samples_scores_path='/hdd0/adaptis_experiments/sbd/multi_click_deeplab_r50/sbd_train_scores.pickle',
        samples_scores_gamma=1.25
    )

    valset = SBDDataset(
        cfg.SBD_PATH,
        split='val',
        augmentator=val_augmentator,
        num_masks=num_masks,
        points_from_one_object=False,
        input_transform=model_cfg.input_transform,
        min_object_area=80,
        image_rescale=scale_func,
        points_sampler=points_sampler
    )

    optimizer_params = {
        'learning_rate': 5e-4,
        'beta1': 0.9, 'beta2': 0.999, 'epsilon': 1e-8
    }
    lr_scheduler = partial(LRScheduler,
                           base_lr=optimizer_params['learning_rate'],
                           mode='step',
                           step_epoch=(100,),
                           nepochs=num_epochs)

    trainer = ISTrainer(model, cfg, model_cfg, loss_cfg,
                        trainset, valset,
                        optimizer='adam',
                        optimizer_params=optimizer_params,
                        lr_scheduler=lr_scheduler,
                        start_epoch=start_epoch,
                        checkpoint_interval=5,
                        image_dump_interval=200,
                        hybridize_model=True,
                        metrics=[AdaptiveIoU()],
                        max_interactive_points=model_cfg.num_max_points)

    logger.info(f'Starting Epoch: {start_epoch}')
    logger.info(f'Total Epochs: {num_epochs}')
    for epoch in range(start_epoch, num_epochs):
        trainer.training(epoch)
        trainer.validation(epoch)
