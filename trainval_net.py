# @Time    : 2018/4/28 22:16
# @File    : trainval_net.py
# @Author  : Sky chen
# @Email   : dzhchxk@126.com
# @Personal homepage  : https://coderskychen.cn

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import _init_paths
import os
import numpy as np
import pprint
import pdb
import time

import torch
from torch.autograd import Variable
import torch.nn as nn
from torch.utils.data.sampler import Sampler

from data_preprocess import ADE
from batchLoader import BatchLoader
from model import weights_normal_init, save_net, load_net, \
    adjust_learning_rate, save_checkpoint, clip_gradient
from model import vgg16, res50
from opts import parse_args
# from visualization import *

try:
    import tensorflow as tf
except ImportError:
    print("Tensorflow not installed; No tensorboard logging.")
    tf = None


def add_summary_value(writer, key, value, iteration):
    summary = tf.Summary(value=[tf.Summary.Value(tag=key, simple_value=value)])
    writer.add_summary(summary, iteration)


def check_rootfolders(trainid):
    """Create log and model folder"""
    folders_util = [args.root_log, args.root_model, args.root_output]
    if not os.path.exists('./data/results'):
        os.makedirs('./data/results')
    for folder in folders_util:
        if not os.path.exists(os.path.join('./data/results', trainid, folder)):
            print('creating folder ' + folder)
            os.makedirs(os.path.join('./data/results', trainid, folder))


if __name__ == '__main__':

    args = parse_args()
    if args.batch_size != 1:
        print('The batch size should always be 1 for now.')
        raise NotImplementedError
    check_rootfolders(args.train_id)
    summary_w = tf and tf.summary.FileWriter(\
        os.path.join('./data/results', args.train_id, args.root_log))  # tensorboard
    print('Called with args:')
    print(args)

    np.random.seed(args.RNG_SEED)

    torch.backends.cudnn.enabled = False  # useful for varying input size (0-0)
    if torch.cuda.is_available() and not args.cuda:
        print("WARNING: You have a CUDA device, \
        so you should probably run with --cuda")

    pd_train = ADE('train', args)
    pd_val = ADE('mval', args)  # without flipper append
    print('{:d} train roidb entries'.format(len(pd_train.roidb)))
    print('{:d} val roidb entries'.format(len(pd_val.roidb)))

    pd_train.filter_roidb()
    pd_val.filter_roidb()

    train_size = len(pd_train.roidb)
    dataset = BatchLoader(pd_train.roidb, args, phase='train')

    dataloader = torch.utils.data.DataLoader(dataset, batch_size=args.batch_size, \
                 num_workers=args.num_workers, shuffle=True)

    dataloader_val = torch.utils.data.DataLoader(BatchLoader(pd_val.roidb, args, phase='eval'), batch_size=1, \
                                             num_workers=args.num_workers, shuffle=False)

    # initilize the tensor holder here.
    im_data = torch.FloatTensor(1)
    im_info = torch.FloatTensor(1)
    num_boxes = torch.LongTensor(1)
    gt_boxes = torch.FloatTensor(1)

    # ship to cuda
    if args.cuda:
        im_data = im_data.cuda()
        im_info = im_info.cuda()
        num_boxes = num_boxes.cuda()
        gt_boxes = gt_boxes.cuda()

    # make variable
    im_data = Variable(im_data)
    im_info = Variable(im_info)
    num_boxes = Variable(num_boxes)
    gt_boxes = Variable(gt_boxes)

    if args.cuda:
        args.CUDA = True

    # initilize the network here.
    if args.net == 'vgg16':
        basenet = vgg16(pd_train.classes, args, pretrained=True)
    elif args.net == 'res50':
        basenet = res50(pd_train.classes, args, pretrained=True)
    else:
        print("network is not defined")
        # pdb.set_trace()

    basenet.create_architecture()

    lr = args.lr

    params = []
    for key, value in dict(basenet.named_parameters()).items():
        if value.requires_grad:
            if 'bias' in key:
                params += [{'params': [value], 'lr': lr * (args.DOUBLE_BIAS + 1), \
                            'weight_decay': args.BIAS_DECAY and args.WEIGHT_DECAY \
                                            or 0}]
            else:
                params += [{'params': [value], 'lr': lr, 'weight_decay': \
                    args.WEIGHT_DECAY}]

    if args.optimizer == "sgd":
        optimizer = torch.optim.SGD(params, momentum=args.MOMENTUM)
    else:
        print('error with optimizer method!')

    if args.resume:
        load_name = os.path.join('data/results', args.train_id, 'model', args.model_name)
        print("loading checkpoint %s" % load_name)
        checkpoint = torch.load(load_name)
        args.start_epoch = checkpoint['epoch']
        basenet.load_state_dict(checkpoint['model'])
        optimizer.load_state_dict(checkpoint['optimizer'])
        lr = optimizer.param_groups[0]['lr']
        print("loaded checkpoint %s" % load_name)

    if args.cuda:
        basenet.cuda()

    iters_per_epoch = int(train_size / args.batch_size)

    total_iters = 1
    total_time = 0.
    for epoch in range(args.start_epoch, args.max_epochs):
        # setting to train mode
        basenet.train()
        loss_temp = 0
        start = time.time()
        data_iter = iter(dataloader)
        for step in range(iters_per_epoch):
            # if step >= 0:  # just for check latter codes
            #     break
            if total_iters % (args.lr_decay_step + 1) == 0:
                adjust_learning_rate(optimizer, args.lr_decay_gamma)
                lr *= args.lr_decay_gamma

            if total_iters > args.max_iters:
                break
            total_iters = total_iters + 1
            data = next(data_iter)
            im_data.data.resize_(data[0].size()).copy_(data[0])
            im_info.data.resize_(data[1].size()).copy_(data[1])
            gt_boxes.data.resize_(data[2].size()).copy_(data[2])

            basenet.zero_grad()
            cls_prob, cls_loss = basenet(im_data, im_info, gt_boxes)

            loss = cls_loss.mean()
            # backward
            optimizer.zero_grad()
            loss.backward()
            if args.net == "vgg16":
                clip_gradient(basenet, 10.)
            optimizer.step()

            if step % args.disp_interval == 0:
                end = time.time()
                loss_data = cls_loss.data[0]

                total_time += end - start

                print(
                    "[epoch %2d][iter %4d/%4d] lr: %.2e; time cost: %f; rcnn_cls: %.4f" % (epoch, step, iters_per_epoch, lr, end - start, loss_data))

                add_summary_value(summary_w, 'loss', loss_data, total_iters)
                add_summary_value(summary_w, 'lr', lr, total_iters)

                start = time.time()

        # eval model every epoch
        data_iter_val = iter(dataloader_val)
        basenet.eval()
        loss_tt = 0.
        all_scores = [[] for _ in range(len(pd_val.roidb))]
        for step in range(len(pd_val.roidb)):
            data = next(data_iter_val)
            im_data.data.resize_(data[0].size()).copy_(data[0])
            im_info.data.resize_(data[1].size()).copy_(data[1])
            gt_boxes.data.resize_(data[2].size()).copy_(data[2])

            cls_prob, cls_loss = basenet(im_data, im_info, gt_boxes)
            # print(cls_prob.size())
            all_scores[step] = cls_prob.data.cpu().numpy()
            loss = cls_loss.mean()
            loss_tt += loss.data[0]

            if step % args.disp_interval == 0:
                end = time.time()
                loss_data = cls_loss.data[0]

                print(
                    "evaling: [epoch %2d][iter %4d/%4d] ; time cost: %f; rcnn_cls: %.4f" % (
                    epoch, step, len(pd_val.roidb), end - start, loss_data))

                start = time.time()

        print('Evaluating detections')
        mcls_sc, mcls_ac, mcls_ap, mins_sc, mins_ac, mins_ap = pd_val.evaluate(all_scores, clip_region=True)
        add_summary_value(summary_w, 'eval_loss', loss_tt/len(pd_val.roidb), total_iters)
        add_summary_value(summary_w, 'mcls_sc', mcls_sc, total_iters)
        add_summary_value(summary_w, 'mcls_ac', mcls_ac, total_iters)
        add_summary_value(summary_w, 'mcls_ap', mcls_ap, total_iters)
        add_summary_value(summary_w, 'mins_sc', mins_sc, total_iters)
        add_summary_value(summary_w, 'mins_ac', mins_ac, total_iters)
        add_summary_value(summary_w, 'mins_ap', mins_ap, total_iters)

        save_name = os.path.join('./data/results', args.train_id, args.root_model,
                                 'checkpoint{}_{}.pth'.format(epoch, total_iters))
        save_checkpoint({
            'train_id': args.train_id,
            'epoch': epoch + 1,
            'model': basenet.state_dict(),
            'optimizer': optimizer.state_dict(),
        }, save_name)
        print('save model: {}'.format(save_name))

        end = time.time()
        print(end - start)

        if total_iters >args.max_iters:
            break

    if args.resume:
        total_iters -= (args.start_epoch - 1) * iters_per_epoch
    print('total train time: %.2f s, %.2f h' % (total_time, total_time / 3600.))
    print('each epoch time: %.2f h' % (total_time / float(total_iters) * iters_per_epoch / 3600.))
    print('each iter time: %.2f s' % (total_time / float(total_iters)))
