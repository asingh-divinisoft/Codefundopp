import torch
from torch import nn
from torch.utils.data import DataLoader
import torch.utils.data.distributed
import torch.optim as optim
import datetime

import os
import argparse
import horovod.torch as hvd

from dataset import WFDataset
from xception import Xception
from azureml.core.run import Run

# get the Azure ML run object
run = Run.get_context()

print("Torch version:", torch.__version__)

# Training settings
parser = argparse.ArgumentParser(description='PyTorch MNIST Example')
parser.add_argument('--data-folder', type=str, dest='data_folder', help='data folder mounting point')
parser.add_argument('--output_dir', type=str, default='./outputs', metavar='O',
                    help='output directory')
parser.add_argument('--batch-size', type=int, default=64, metavar='N',
                    help='input batch size for training (default: 64)')
parser.add_argument('--test-batch-size', type=int, default=1000, metavar='N',
                    help='input batch size for testing (default: 1000)')
parser.add_argument('--epochs', type=int, default=10, metavar='N',
                    help='number of epochs to train (default: 10)')
parser.add_argument('--lr', type=float, default=0.01, metavar='LR',
                    help='learning rate (default: 0.01)')
parser.add_argument('--momentum', type=float, default=0.5, metavar='M',
                    help='SGD momentum (default: 0.5)')
parser.add_argument('--no-cuda', action='store_true', default=False,
                    help='disables CUDA training')
parser.add_argument('--seed', type=int, default=42, metavar='S',
                    help='random seed (default: 42)')
parser.add_argument('--log-interval', type=int, default=10, metavar='N',
                    help='how many batches to wait before logging training status')
parser.add_argument('--fp16-allreduce', action='store_true', default=False,
                    help='use fp16 compression during allreduce')
args = parser.parse_args()


args.cuda = not args.no_cuda and torch.cuda.is_available()

hvd.init()
torch.manual_seed(args.seed)

if args.cuda:
    # Horovod: pin GPU to local rank.
    torch.cuda.set_device(hvd.local_rank())
    torch.cuda.manual_seed(args.seed)


kwargs = {'num_workers': 2, 'pin_memory': False} if args.cuda else {}

data_folder = os.path.join(args.data_folder, 'data_wf')
datasets =WFDataset(data_folder)

train_dataset = datasets.train_ds
train_sampler = torch.utils.data.distributed.DistributedSampler(
    train_dataset, num_replicas=hvd.size(), rank=hvd.rank())
train_loader = torch.utils.data.DataLoader(
    train_dataset, batch_size=args.batch_size, sampler=train_sampler, **kwargs)

test_dataset = datasets.test_ds
test_sampler = torch.utils.data.distributed.DistributedSampler(
    test_dataset, num_replicas=hvd.size(), rank=hvd.rank())
test_loader = torch.utils.data.DataLoader(test_dataset, batch_size=args.test_batch_size,
                                          sampler=test_sampler, **kwargs)

PATH = os.path.join(args.data_folder, 'weights', 'xception_imagenet.pth')

def time():
    now = datetime.datetime.now()
    name = f'{now.date()}_{now.hour}-{now.minute}'
    return name

N_FINETUNE_EPOCHS = 2
CKPT = 'xception_train'
model = Xception()
model.load_scratch(PATH)

if args.cuda:
    # Move model to GPU.
    model.cuda()

# Horovod: broadcast parameters.
hvd.broadcast_parameters(model.state_dict(), root_rank=0)

# Horovod: scale learning rate by the number of GPUs.
optimizer = optim.SGD(model.parameters(), lr=args.lr * hvd.size(),
                      momentum=args.momentum)

# Horovod: (optional) compression algorithm.
compression = hvd.Compression.fp16 if args.fp16_allreduce else hvd.Compression.none

# Horovod: wrap optimizer with DistributedOptimizer.
optimizer = hvd.DistributedOptimizer(optimizer,
                                     named_parameters=model.named_parameters(),
                                     compression=compression)


class_weights = torch.Tensor(datasets.classw).cuda()
criterion = nn.CrossEntropyLoss(weight=class_weights)
val_criterion = nn.CrossEntropyLoss(weight=class_weights, size_average=False)

def train(epoch):
    model.train()
    train_sampler.set_epoch(epoch)
    for batch_idx, (data, target) in enumerate(train_loader):
        if args.cuda:
            data, target = data.cuda(), target.cuda()
        optimizer.zero_grad()
        output = model(data)
        loss = criterion(output, target)
        loss.backward()
        optimizer.step()

        if batch_idx % args.log_interval == 0:
            print('Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}'.format(
                epoch, batch_idx * len(data), len(train_sampler),
                100. * batch_idx / len(train_loader), loss.item()))

            # log the loss to the Azure ML run
            run.log('loss', loss.item())


def metric_average(val, name):
    tensor = torch.tensor(val)
    avg_tensor = hvd.allreduce(tensor, name=name)
    return avg_tensor.item()


def test():
    model.eval()
    test_loss = 0.
    test_accuracy = 0.
    for data, target in test_loader:
        if args.cuda:
            data, target = data.cuda(), target.cuda()
        output = model(data)
        # sum up batch loss
        test_loss += val_criterion(output, target).item()
        # get the index of the max log-probability
        pred = output.data.max(1, keepdim=True)[1]
        test_accuracy += pred.eq(target.data.view_as(pred)).cpu().float().sum()

    test_loss /= len(test_sampler)
    test_accuracy /= len(test_sampler)

    test_loss = metric_average(test_loss, 'avg_loss')
    test_accuracy = metric_average(test_accuracy, 'avg_accuracy')

    run.log('val_loss', test_loss)
    run.log('val_acc', test_accuracy)

    if hvd.rank() == 0:
        print('\nTest set: Average loss: {:.4f}, Accuracy: {:.2f}%\n'.format(
            test_loss, 100. * test_accuracy))


for epoch in range(1, N_FINETUNE_EPOCHS):
    train(epoch)
    test()
    torch.save(model.state_dict(), os.path.join(args.output_dir, f'{CKPT}_{time()}.pth'))

for param in model.features.parameters():
    param.requires_grad = True

for epoch in range(N_FINETUNE_EPOCHS, args.epochs + 1):
    train(epoch)
    test()
    torch.save(model.state_dict(), os.path.join(args.output_dir, f'{CKPT}_{time()}.pth'))

