import argparse
import torch
import imageio
import skimage.transform
import torchvision

import torch.optim
from models import DFSeg_model, ACNet_models, AsymNet, RedNet_model
from utils import utils
from torch.utils.data import Dataset,DataLoader
from dataset.seg_data import *
import os
import datetime

import torch.nn as nn
import glob,pdb
import scipy.io as sio
from utils.utils import load_ckpt, intersectionAndUnion, AverageMeter, accuracy, macc

parser = argparse.ArgumentParser(description='RGBD Sementic Segmentation')
parser.add_argument('--data-dir', default=None, metavar='DIR',
                    help='path to dataset')
parser.add_argument('-o', '--output', default='./result/', metavar='DIR',
                    help='path to output')
parser.add_argument('--cuda', action='store_true', default=False,
                    help='enables CUDA training')
parser.add_argument('--last-ckpt', default='./model/ckpt_epoch_20.00.pth', type=str, metavar='PATH',
                    help='path to latest checkpoint (default: none)')
parser.add_argument('--num-class', default=40, type=int,
                    help='number of classes')
parser.add_argument('--visualize', default=False, action='store_true',
                    help='if output image')

args = parser.parse_args()
device = torch.device("cuda:0" if args.cuda and torch.cuda.is_available() else "cpu")
image_w = 640
image_h = 480


train_file = '/home/mit/shadow/nyuv2/train.txt'
test_file = '/home/mit/shadow/nyuv2/test.txt'

def make_dataset_fromlst(listfilename):
    """
    NYUlist format:
    imagepath seglabelpath depthpath HHApath
    """
    images = []
    segs = []
    depths = []
    with open(listfilename) as f:
        content = f.readlines()
        for x in content:
            imgname, depthname, segname = x.strip().split(' ')
            imgname = '/home/mit/shadow/' + imgname
            depthname = '/home/mit/shadow/' + depthname
            segname = '/home/mit/shadow/' + segname
            images += [imgname]
            segs += [segname]
            depths += [depthname]
    return {'images':images, 'labels':segs, 'depths':depths}

class SUNRGBD(Dataset):
    def __init__(self, transform=None, phase_train=True, data_dir=None):

        self.phase_train = phase_train
        self.transform = transform

        result = make_dataset_fromlst(train_file)
        self.img_dir_train = result['images']
        self.depth_dir_train = result['depths']
        self.label_dir_train = result['labels']

        result = make_dataset_fromlst(test_file)
        self.img_dir_test = result['images']
        self.depth_dir_test = result['depths']
        self.label_dir_test = result['labels']

    def __len__(self):
        if self.phase_train:
            return len(self.img_dir_train)
        else:
            return len(self.img_dir_test)

    def __getitem__(self, idx):
        if self.phase_train:
            img_dir = self.img_dir_train
            depth_dir = self.depth_dir_train
            label_dir = self.label_dir_train
        else:
            img_dir = self.img_dir_test
            depth_dir = self.depth_dir_test
            label_dir = self.label_dir_test

        label = np.load(label_dir[idx])
        depth = np.load(depth_dir[idx])
        image = np.load(img_dir[idx])

        sample = {'image': image, 'depth': depth, 'label': label}

        if self.transform:
            sample = self.transform(sample)

        return sample


class RandomHSV(object):
    """
        Args:
            h_range (float tuple): random ratio of the hue channel,
                new_h range from h_range[0]*old_h to h_range[1]*old_h.
            s_range (float tuple): random ratio of the saturation channel,
                new_s range from s_range[0]*old_s to s_range[1]*old_s.
            v_range (int tuple): random bias of the value channel,
                new_v range from old_v-v_range to old_v+v_range.
        Notice:
            h range: 0-1
            s range: 0-1
            v range: 0-255
        """

    def __init__(self, h_range, s_range, v_range):
        assert isinstance(h_range, (list, tuple)) and \
               isinstance(s_range, (list, tuple)) and \
               isinstance(v_range, (list, tuple))
        self.h_range = h_range
        self.s_range = s_range
        self.v_range = v_range

    def __call__(self, sample):
        img = sample['image']
        img_hsv = matplotlib.colors.rgb_to_hsv(img)
        img_h, img_s, img_v = img_hsv[:, :, 0], img_hsv[:, :, 1], img_hsv[:, :, 2]
        h_random = np.random.uniform(min(self.h_range), max(self.h_range))
        s_random = np.random.uniform(min(self.s_range), max(self.s_range))
        v_random = np.random.uniform(-min(self.v_range), max(self.v_range))
        img_h = np.clip(img_h * h_random, 0, 1)
        img_s = np.clip(img_s * s_random, 0, 1)
        img_v = np.clip(img_v + v_random, 0, 255)
        img_hsv = np.stack([img_h, img_s, img_v], axis=2)
        img_new = matplotlib.colors.hsv_to_rgb(img_hsv)

        return {'image': img_new, 'depth': sample['depth'], 'label': sample['label']}


class scaleNorm(object):
    def __call__(self, sample):
        image, depth, label = sample['image'], sample['depth'], sample['label']

        # Bi-linear
        image = skimage.transform.resize(image, (image_h, image_w), order=1,
                                         mode='reflect', preserve_range=True)
        # Nearest-neighbor
        depth = skimage.transform.resize(depth, (image_h, image_w), order=0,
                                         mode='reflect', preserve_range=True)
        label = skimage.transform.resize(label, (image_h, image_w), order=0,
                                         mode='reflect', preserve_range=True)

        return {'image': image, 'depth': depth, 'label': label}


class RandomScale(object):
    def __init__(self, scale):
        self.scale_low = min(scale)
        self.scale_high = max(scale)

    def __call__(self, sample):
        image, depth, label = sample['image'], sample['depth'], sample['label']

        target_scale = random.uniform(self.scale_low, self.scale_high)
        # (H, W, C)
        target_height = int(round(target_scale * image.shape[0]))
        target_width = int(round(target_scale * image.shape[1]))
        # Bi-linear
        image = skimage.transform.resize(image, (target_height, target_width),
                                         order=1, mode='reflect', preserve_range=True)
        # Nearest-neighbor
        depth = skimage.transform.resize(depth, (target_height, target_width),
                                         order=0, mode='reflect', preserve_range=True)
        label = skimage.transform.resize(label, (target_height, target_width),
                                         order=0, mode='reflect', preserve_range=True)

        return {'image': image, 'depth': depth, 'label': label}


class RandomCrop(object):
    def __init__(self, th, tw):
        self.th = th
        self.tw = tw

    def __call__(self, sample):
        image, depth, label = sample['image'], sample['depth'], sample['label']
        h = image.shape[0]
        w = image.shape[1]
        i = random.randint(0, h - self.th)
        j = random.randint(0, w - self.tw)

        return {'image': image[i:i + image_h, j:j + image_w, :],
                'depth': depth[i:i + image_h, j:j + image_w],
                'label': label[i:i + image_h, j:j + image_w]}


class RandomFlip(object):
    def __call__(self, sample):
        image, depth, label = sample['image'], sample['depth'], sample['label']
        if random.random() > 0.5:
            image = np.fliplr(image).copy()
            depth = np.fliplr(depth).copy()
            label = np.fliplr(label).copy()

        return {'image': image, 'depth': depth, 'label': label}

# Transforms on torch.*Tensor
class Normalize(object):
    def __call__(self, sample):
        image, depth = sample['image'], sample['depth']
        image = image / 255
        image = torchvision.transforms.Normalize(mean=[0.4850042694973687, 0.41627756261047333, 0.3981809741523051],
                                                 std=[0.26415541082494515, 0.2728415392982039, 0.2831175140191598])(image)
        depth = torchvision.transforms.Normalize(mean=[2.8424503515351494],
                                                 std=[0.9932836506164299])(depth)
        sample['image'] = image
        sample['depth'] = depth

        return sample


class ToTensor(object):
    """Convert ndarrays in sample to Tensors."""

    def __call__(self, sample):
        image, depth, label = sample['image'], sample['depth'], sample['label']

        # Generate different label scales
        label2 = skimage.transform.resize(label, (label.shape[0] // 2, label.shape[1] // 2),
                                          order=0, mode='reflect', preserve_range=True)
        label3 = skimage.transform.resize(label, (label.shape[0] // 4, label.shape[1] // 4),
                                          order=0, mode='reflect', preserve_range=True)
        label4 = skimage.transform.resize(label, (label.shape[0] // 8, label.shape[1] // 8),
                                          order=0, mode='reflect', preserve_range=True)
        label5 = skimage.transform.resize(label, (label.shape[0] // 16, label.shape[1] // 16),
                                          order=0, mode='reflect', preserve_range=True)

        # swap color axis because
        # numpy image: H x W x C
        # torch image: C X H X W
        image = image.transpose((2, 0, 1))
        depth = np.expand_dims(depth, 0).astype(np.float)
        return {'image': torch.from_numpy(image).float(),
                'depth': torch.from_numpy(depth).float(),
                'label': torch.from_numpy(label).float(),
                'label2': torch.from_numpy(label2).float(),
                'label3': torch.from_numpy(label3).float(),
                'label4': torch.from_numpy(label4).float(),
                'label5': torch.from_numpy(label5).float()}

def inference():
    model = DFSeg_model.RedNet(num_classes=40, pretrained=False)
    #model = nn.DataParallel(model)
    load_ckpt(model, None, args.last_ckpt, device)
    model.eval()
    model.to(device)

    val_data = SUNRGBD(transform=torchvision.transforms.Compose([scaleNorm(),
                                                                   ToTensor(),
                                                                   Normalize()]),
                                   phase_train=False,
                                   data_dir=args.data_dir
                                   )
    val_loader = DataLoader(val_data, batch_size=1, shuffle=False,num_workers=1, pin_memory=True)

    acc_meter = AverageMeter()
    intersection_meter = AverageMeter()
    union_meter = AverageMeter()
    a_meter = AverageMeter()
    b_meter = AverageMeter()
    with torch.no_grad():
        for batch_idx, sample in enumerate(val_loader):
            #origin_image = sample['origin_image'].numpy()
            #origin_depth = sample['origin_depth'].numpy()
            image = sample['image'].to(device)
            depth = sample['depth'].to(device)
            label = sample['label'].numpy()

            with torch.no_grad():
                pred = model(image, depth)

            output = torch.max(pred, 1)[1] + 1
            output = output.squeeze(0).cpu().numpy()

            acc, pix = accuracy(output, label)
            intersection, union = intersectionAndUnion(output, label, args.num_class)
            acc_meter.update(acc, pix)
            a_m, b_m = macc(output, label, args.num_class)
            intersection_meter.update(intersection)
            union_meter.update(union)
            a_meter.update(a_m)
            b_meter.update(b_m)
            print('[{}] iter {}, accuracy: {}'
                  .format(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                          batch_idx, acc))

            # img = image.cpu().numpy()
            # print('origin iamge: ', type(origin_image))
            #if args.visualize:
            #    visualize_result(origin_image, origin_depth, label-1, output-1, batch_idx, args)

    iou = intersection_meter.sum / (union_meter.sum + 1e-10)
    for i, _iou in enumerate(iou):
        print('class [{}], IoU: {}'.format(i, _iou))

    mAcc = (a_meter.average() / (b_meter.average()+1e-10))
    print(mAcc.mean())
    print('[Eval Summary]:')
    print('Mean IoU: {:.4}, Accuracy: {:.2f}%'
          .format(iou.mean(), acc_meter.average() * 100))
        # imageio.imsave(args.output, output.cpu().numpy().transpose((1, 2, 0)))

if __name__ == '__main__':
    if not os.path.exists(args.output):
        os.mkdir(args.output)
    inference()
