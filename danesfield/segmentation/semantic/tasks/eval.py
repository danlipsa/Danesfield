###############################################################################
# Copyright Kitware Inc. and Contributors
# Distributed under the Apache License, 2.0 (apache.org/licenses/LICENSE-2.0)
# See accompanying Copyright.txt and LICENSE files for details
###############################################################################

import os
import cv2
import numpy as np
from scipy.spatial.distance import dice
import torch
import torch.nn.functional as F
from danesfield.segmentation.semantic.models import resnet_unet
from danesfield.segmentation.semantic.models import dense_unet
import tqdm


from danesfield.segmentation.semantic.dataset.neural_dataset import ValDataset, SequentialDataset
from torch.utils.data.dataloader import DataLoader as PytorchDataLoader
from utils.utils import heatmap
from torch.serialization import SourceChangeWarning
import warnings


class flip:
    """
    flip types for TTA
    """
    FLIP_NONE = 0
    FLIP_LR = 1
    FLIP_FULL = 2


def flip_tensor_lr(batch):
    columns = batch.data.size()[-1]
    return batch.index_select(3, torch.LongTensor(list(reversed(range(columns)))).cuda())


def flip_tensor_ud(batch):
    rows = batch.data.size()[-2]
    return batch.index_select(2, torch.LongTensor(list(reversed(range(rows)))).cuda())


def to_numpy(batch):
    if isinstance(batch, tuple):
        batch = batch[0]
    return F.sigmoid(batch).data.cpu().numpy()


def predict(model, batch, flips=flip.FLIP_NONE):
    pred1 = model(batch)
    if flips > flip.FLIP_NONE:
        pred2 = flip_tensor_lr(model(flip_tensor_lr(batch)))
        masks = [pred1, pred2]
        if flips > flip.FLIP_LR:
            pred3 = flip_tensor_ud(model(flip_tensor_ud(batch)))
            pred4 = flip_tensor_ud(flip_tensor_lr(model(flip_tensor_ud(flip_tensor_lr(batch)))))
            masks.extend([pred3, pred4])
        new_mask = torch.mean(torch.stack(masks, 0), 0)
        return to_numpy(new_mask)
        # p1 = to_numpy(pred1)
        # p2 = to_numpy(pred2)
        # new_mask = np.sqrt(p1 * p2)
        # return new_mask
    return to_numpy(pred1)


def read_model(config, fold):
    # model = nn.DataParallel(torch.load(os.path.join('..', 'weights', project,
    # 'fold{}_best.pth'.format(fold))))
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', SourceChangeWarning)
        model = torch.load(os.path.join(config.results_dir, 'weights', config.folder,
                           'fold{}_best.pth'.format(fold)))
        model.eval()
        return model


def read_onetrain_model(config):
    # model = nn.DataParallel(torch.load(os.path.join('..', 'weights', project,
    #  'fold{}_best.pth'.format(fold))))
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', SourceChangeWarning)
        model_path = config.pretrain_model_path

        model = None
        if '_checkpoint' in model_path:
            checkpoint = torch.load(os.path.join(config.results_dir, 'weights', config.folder,
                                                 'onetrain_checkpoint.pth'))
            if config.folder == 'resnet34_':
                model = resnet_unet.ResnetUNet(num_classes=1, num_channels=5)
            elif config.folder == 'denseunet_':
                model = dense_unet.DenseUNet(in_channels=5, n_classes=1)

            model = torch.nn.DataParallel(model).cuda()
            pretrained_dict = checkpoint['state_dict']
            model_dict = model.state_dict()
            model_dict.update(pretrained_dict)

        else:
            model = torch.load(model_path)

        model.eval()
        return model


class Evaluator:
    """
    base class for inference, supports different strategy - full image or crops,
    also supports different image types
    """
    def __init__(self, config, ds, folds, test=False, flips=0, num_workers=0, border=12):
        self.config = config
        self.ds = ds
        self.folds = folds
        self.test = test
        self.flips = flips
        self.num_workers = num_workers

        self.full_image = None
        self.full_mask = None
        self.current_mask = None
        self.full_pred = None
        self.border = border
        self.folder = config.folder
        self.prev_name = None
        self.on_new = False
        self.show_mask = config.dbg
        self.need_dice = False
        self.dice = []

        if self.config.save_images:
            if not os.path.exists(self.config.results_dir):
                os.makedirs(self.config.results_dir, exist_ok=True)

    def visualize(self, show_light=False, show_base=True):
        dsize = None
        hmap = heatmap(self.full_pred)
        if self.full_image is not None and show_light:
            light_heat = cv2.addWeighted(self.full_image[:, :, :3], 0.6, hmap, 0.4, 0)
            if dsize:
                light_heat = cv2.resize(light_heat, (dsize, dsize))
            cv2.imshow('light heat', light_heat)
            if self.full_mask is not None and self.show_mask:
                light_mask = cv2.addWeighted(self.full_image[:, :, :3], 0.6,
                                             cv2.cvtColor(self.full_mask,
                                             cv2.COLOR_GRAY2BGR), 0.4, 0)
                if dsize:
                    light_mask = cv2.resize(light_mask, (dsize, dsize))
                cv2.imshow('light mask', light_mask)
        if self.full_image is not None and show_base:
            if dsize:
                cv2.imshow('image', cv2.resize(self.full_image[:, :, :3], (dsize, dsize)))
            else:
                cv2.imshow('image', self.full_image[:, :, :3])
            if dsize:
                hmap = cv2.resize(hmap, (dsize, dsize))
            cv2.imshow('heatmap', hmap)
            if self.full_mask is not None and self.show_mask:
                if dsize:
                    cv2.imshow('mask', cv2.resize(self.full_mask, (dsize, dsize)))
                else:
                    cv2.imshow('mask', self.full_mask)
        if show_light or show_base:
            cv2.waitKey()

    def predict(self, skip_folds=None):
        for fold, (train_index, val_index) in enumerate(self.folds):
            prefix = ('fold' + str(fold) + "_") if self.test else ""
            if skip_folds is not None:
                if fold in skip_folds:
                    continue
            self.prev_name = None
            ds_cls = ValDataset if not self.test else SequentialDataset
            val_dataset = ds_cls(self.ds, val_index, stage='test', config=self.config)
            val_dl = PytorchDataLoader(val_dataset, batch_size=self.config.predict_batch_size,
                                       num_workers=self.num_workers, drop_last=False)
            model = read_model(self.config, fold)
            pbar = val_dl if self.config.dbg else tqdm.tqdm(val_dl, total=len(val_dl))
            for data in pbar:
                self.show_mask = 'mask' in data and self.show_mask
                if 'mask' not in data:
                    self.need_dice = False

                predicted = self.predict_samples(model, data)
                self.process_data(predicted, model, data, prefix=prefix)

                if not self.config.dbg and self.need_dice:
                    pbar.set_postfix(dice="{:.5f}".format(np.mean(self.dice)))
            self.on_image_constructed(prefix=prefix)
            if self.need_dice:
                print(np.mean(self.dice))

    def onepredict(self):
        prefix = 'onetrain' if self.test else ""
        self.prev_name = None
        ds_cls = ValDataset if not self.test else SequentialDataset
        val_index = [i for i in range(4)]
        val_dataset = ds_cls(self.ds, val_index, stage='test', config=self.config)
        val_dl = PytorchDataLoader(val_dataset, batch_size=self.config.test_batch_size,
                                   num_workers=self.num_workers, drop_last=False)
        model = read_onetrain_model(self.config)
        pbar = val_dl if self.config.dbg else tqdm.tqdm(val_dl, total=len(val_dl))
        for data in pbar:
            self.show_mask = 'mask' in data and self.show_mask
            if 'mask' not in data:
                self.need_dice = False

            # print('onepredict data: {}'.format(data))
            predicted = self.predict_samples_large(model, data)
            self.process_data(predicted, model, data, prefix=prefix)

            if not self.config.dbg and self.need_dice:
                pbar.set_postfix(dice="{:.5f}".format(np.mean(self.dice)))
        self.on_image_constructed(prefix=prefix)
        if self.need_dice:
            print(np.mean(self.dice))

    def cut_border(self, image):
        return image if not self.border else image[self.border:-self.border,
                                                   self.border:-self.border, ...]

    def on_image_constructed(self, prefix=""):
        """
        mostly used for crops, but can be used on full image
        """
        self.full_pred = self.cut_border(self.full_pred)
        if self.full_image is not None:
            self.full_image = self.cut_border(self.full_image)
        if self.full_mask is not None:
            self.full_mask = self.cut_border(self.full_mask)
            if np.any(self.full_pred > .5) or np.any(self.full_mask >= 1):
                d = 1 - dice(self.full_pred.flatten() > .5, self.full_mask.flatten() >= 1)
                self.dice.append(d)
                if self.config.dbg:
                    print(self.prev_name, ' dice: ', d)
            else:
                return

        # print(self.prev_name)
        if self.config.dbg:
            self.visualize(show_light=True)
        if self.config.save_images:
            self.save(self.prev_name, prefix=prefix)

    def predict_samples(self, model, data):
        samples = torch.autograd.Variable(data['image'], volatile=True).cuda()
        predicted = predict(model, samples, flips=self.flips)
        return predicted

    def predict_samples_2048x2048(self, model, data):
        dsize = data['image'].size()
        if dsize[-1] != 2048 and dsize[-2] != 2048:
            print('The image size is not 2048x2048. Wrong!')
            return None

        insidx = [0, 512, 1024]
        ineidx = [1024, 1536, 2048]
        cpsidx = [0, 288, 224]
        cpeidx = [800, 736, 1024]
        outsidx = [0, 800, 1248]
        outeidx = [800, 1248, 2048]

        predicted = np.zeros((dsize[0], 1, dsize[2], dsize[3]))
        for i in range(3):
            for j in range(3):
                samples = torch.autograd.Variable(data['image'][:, :, insidx[i]:ineidx[i],
                                                  insidx[j]:ineidx[j]], volatile=True).cuda()
                prediction = predict(model, samples, flips=self.flips)
                predicted[:, :, outsidx[i]:outeidx[i], outsidx[j]:outeidx[j]
                          ] = prediction[:, :, cpsidx[i]:cpeidx[i], cpsidx[j]:cpeidx[j]]
                # predicted[:,0:1, (i*width):((i+1)*width), (j*width):((j+1)*width)
                #           ] = predict(model, samples, flips=self.flips)

        return predicted

    def index_in_copy_out(self, width):
        if width < 1024:
            print('Width is smaller than 1024. This is not enough to split')
            return None

        nwids = int(np.ceil(width/1024.0))
        cpwid = int(width/nwids)

        if cpwid % 2 == 1:
            cpwid += 1

        insidx = [0]
        ineidx = [1024]
        cpsidx = [0]
        cpeidx = [cpwid]
        outsidx = [0]
        outeidx = [cpwid]

        for i in range(1, nwids-1, 1):
            ins = int((i+0.5)*cpwid - 512)
            ine = int(ins + 1024)
            cps = int(512-cpwid*0.5)
            cpe = int(512+cpwid*0.5)
            outs = int(cpwid*i)
            oute = int(cpwid*(i+1))

            insidx.append(ins)
            ineidx.append(ine)
            cpsidx.append(cps)
            cpeidx.append(cpe)
            outsidx.append(outs)
            outeidx.append(oute)

        rest = width - (nwids-1)*cpwid
        insidx.append(width-1024)
        ineidx.append(width)
        cpsidx.append(1024-rest)
        cpeidx.append(1024)
        outsidx.append(width-rest)
        outeidx.append(width)

        return insidx, ineidx, cpsidx, cpeidx, outsidx, outeidx

    def predict_samples_large(self, model, data):
        dsize = data['image'].size()

        xinsidx, xineidx, xcpsidx, xcpeidx, xoutsidx, xouteidx = self.index_in_copy_out(dsize[2])
        yinsidx, yineidx, ycpsidx, ycpeidx, youtsidx, youteidx = self.index_in_copy_out(dsize[3])

#        print('image size: {}'.format((dsize[2], dsize[3])))
#        print('xinsdix: {}'.format(xinsidx))
#        print('xinedix: {}'.format(xineidx))
#        print('yinsdix: {}'.format(yinsidx))
#        print('yinedix: {}'.format(yineidx))
#        print('xcpsdix: {}'.format(xcpsidx))
#        print('xcpedix: {}'.format(xcpeidx))
#        print('ycpsdix: {}'.format(ycpsidx))
#        print('ycpedix: {}'.format(ycpeidx))
#        print('xoutsdix: {}'.format(xoutsidx))
#        print('xoutedix: {}'.format(xouteidx))
#        print('youtsdix: {}'.format(youtsidx))
#        print('youtedix: {}'.format(youteidx))

        predicted = np.zeros((dsize[0], 1, dsize[2], dsize[3]))
        for i in range(len(xinsidx)):
            for j in range(len(yinsidx)):
                samples = torch.autograd.Variable(data['image'][:, :, xinsidx[i]:xineidx[i],
                                                  yinsidx[j]:yineidx[j]], volatile=True).cuda()
                prediction = predict(model, samples, flips=self.flips)
                predicted[:, :, xoutsidx[i]:xouteidx[i], youtsidx[j]:youteidx[j]
                          ] = prediction[:, :, xcpsidx[i]:xcpeidx[i], ycpsidx[j]:ycpeidx[j]]

        return predicted

    def get_data(self, data):
        """
        transform data to viewable representation
        """
        names = data['image_name']
        samples = data['image'].numpy()

        if self.need_dice or self.show_mask:
            masks = data['mask'].numpy()
            masks = np.moveaxis(masks, 1, -1)
        else:
            masks = None
        if self.config.dbg:
            samples = np.moveaxis(samples, 1, -1)
        else:
            samples = None

        return names, samples, masks

    def save(self, name, prefix=""):
        raise NotImplementedError

    def process_data(self, predicted, model, data, prefix=""):
        raise NotImplementedError
