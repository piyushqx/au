import collections
import numpy as np

import chainer
import chainer.functions as F
import chainer.links as L
from chainer.links import ResNet101Layers
import functools
from AU_rcnn.links.model.faster_rcnn.faster_rcnn import FasterRCNN
import config
from chainer import initializers

class FasterRCNNResnet101(FasterRCNN):

    """Faster R-CNN based on ResNet101.

    When you specify the path of a pre-trained chainer model serialized as
    a :obj:`.npz` file in the constructor, this chain model automatically
    initializes all the parameters with it.
    When a string in prespecified set is provided, a pretrained model is
    loaded from weights distributed on the Internet.
    The list of pretrained models supported are as follows:

    * :obj:`voc07`: Loads weights trained with the trainval split of \
        PASCAL VOC2007 Detection Dataset.
    * :obj:`imagenet`: Loads weights trained with ImageNet Classfication \
        task for the feature extractor and the head modules. \
        Weights that do not have a corresponding layer in VGG-16 \
        will be randomly initialized.

    For descriptions on the interface of this model, please refer to
    :class:`AU_rcnn.links.model.faster_rcnn.FasterRCNN`.

    :obj:`FasterRCNNVGG16` supports finer control on random initializations of
    weights by arguments
    :obj:`vgg_initialW`, :obj:`rpn_initialW`, :obj:`loc_initialW` and
    :obj:`score_initialW`.
    It accepts a callable that takes an array and edits its values.
    If :obj:`None` is passed as an initializer, the default initializer is
    used.

    Args:
        n_fg_class (int): The number of classes excluding the background.
        pretrained_model (str): The destination of the pre-trained
            chainer model serialized as a :obj:`.npz` file.
            If this is one of the strings described
            above, it automatically loads weights stored under a directory
            :obj:`$CHAINER_DATASET_ROOT/pfnet/AU_rcnn/models/`,
            where :obj:`$CHAINER_DATASET_ROOT` is set as
            :obj:`$HOME/.chainer/dataset` unless you specify another value
            by modifying the environment variable.
        min_size (int): A preprocessing paramter for :meth:`prepare`.
        max_size (int): A preprocessing paramter for :meth:`prepare`.
        ratios (list of floats): This is ratios of width to height of
            the anchors.
        anchor_scales (list of numbers): This is areas of anchors.
            Those areas will be the product of the square of an element in
            :obj:`anchor_scales` and the original area of the reference
            window.
        vgg_initialW (callable): Initializer for the layers corresponding to
            the VGG-16 layers.
        rpn_initialW (callable): Initializer for Region Proposal Network
            layers.
        loc_initialW (callable): Initializer for the localization head.
        score_initialW (callable): Initializer for the score head.
        proposal_creator_params (dict): Key valued paramters for
            :obj:`AU_rcnn.links.model.faster_rcnn.ProposalCreator`.

    """

    _models = {
        'voc07': {
            'n_fg_class': 20,
            'url': 'https://github.com/yuyu2172/share-weights/releases/'
            'download/0.0.3/faster_rcnn_vgg16_voc07_2017_06_06.npz'
        },
        'imagenet': {
            'path': "{}/caffe_model/VGG_ILSVRC_16_layers.npz".format(config.ROOT_PATH)
            # 'url': "http://www.robots.ox.ac.uk/%7Evgg/software/very_deep/caffe/VGG_ILSVRC_16_layers.caffemodel"
        },
        'resnet101': {
            'path': config.ROOT_PATH + '/caffe_model/ResNet-101-model.npz'
        }
    }

    feat_stride = 16

    def __init__(self,
                 n_fg_class=None,
                 pretrained_model=None,
                 min_size=512, max_size=512,
                 fc_initialW=None,
                 score_initialW=None,
                 mean_file=None,
                 extract_len=None,
                 fix=False
                 ):
        if n_fg_class is None:
            if pretrained_model not in self._models:
                raise ValueError(
                    'The n_fg_class needs to be supplied as an argument')
            # n_fg_class = self._models[pretrained_model]['n_fg_class'] # ?????????????????????????????????,??????n_fg_class??????AU????????????
            n_fg_class = len(config.AU_SQUEEZE)
        if score_initialW is None:
            score_initialW = chainer.initializers.Normal(0.01)
        if fc_initialW is None and pretrained_model:
            fc_initialW = chainer.initializers.constant.Zero()

        extractor = ResnetFeatureExtractor(fix=fix)
        self.extract_len = extract_len
        head = ResRoIHead(
            n_fg_class,  # ??????:???0???????????????010101????????????label??????????????????????????????0??????????????????
            roi_size=14, spatial_scale=1. / self.feat_stride, # 1/ 16.0 means after extract feature map, the map become 1/16 of original image, ROI bbox also needs shrink
            fc_initialW=fc_initialW,
            score_initialW=score_initialW,
            extract_len=extract_len
        )
        mean_array = None
        if mean_file is not None:
            mean_array = np.load(mean_file)
        print("loading mean_file in: {} done".format(mean_file))
        super(FasterRCNNResnet101, self).__init__(
            extractor,
            head,
            mean=mean_array,
            min_size=min_size,
            max_size=max_size
        )

        if pretrained_model == 'resnet101':  # ??????????????????elif???
            self._copy_imagenet_pretrained_resnet101(path=self._models['resnet101']['path'])
            print("load pretrained file: {} done".format(self._models['resnet101']['path']))
        elif pretrained_model.endswith(".npz"):
            print("loading :{} to AU R-CNN ResNet-101".format(pretrained_model))
            chainer.serializers.load_npz(pretrained_model, self)

    def _copy_imagenet_pretrained_resnet101(self, path):
        pretrained_model = ResNet101Layers(pretrained_model=path)
        self.extractor.conv1.copyparams(pretrained_model.conv1)
        self.extractor.bn1.copyparams(pretrained_model.bn1)
        self.extractor.res2.copyparams(pretrained_model.res2)
        self.extractor.res3.copyparams(pretrained_model.res3)
        self.extractor.res4.copyparams(pretrained_model.res4)
        self.head.res5.copyparams(pretrained_model.res5)
        if self.extract_len is not None and self.extract_len == 1000:
            self.head.fc.copyparams(pretrained_model.fc6)



class BottleNeckA(chainer.Chain):

    def __init__(self, in_size, ch, out_size, stride=2):
        super(BottleNeckA, self).__init__()
        initialW = initializers.HeNormal()

        with self.init_scope():
            self.conv1 = L.Convolution2D(
                in_size, ch, 1, stride, 0, initialW=initialW, nobias=True)
            self.bn1 = L.BatchNormalization(ch)
            self.conv2 = L.Convolution2D(
                ch, ch, 3, 1, 1, initialW=initialW, nobias=True)
            self.bn2 = L.BatchNormalization(ch)
            self.conv3 = L.Convolution2D(
                ch, out_size, 1, 1, 0, initialW=initialW, nobias=True)
            self.bn3 = L.BatchNormalization(out_size)

            self.conv4 = L.Convolution2D(
                in_size, out_size, 1, stride, 0,
                initialW=initialW, nobias=True)
            self.bn4 = L.BatchNormalization(out_size)

    def __call__(self, x):
        h1 = F.relu(self.bn1(self.conv1(x)))
        h1 = F.relu(self.bn2(self.conv2(h1)))
        h1 = self.bn3(self.conv3(h1))
        h2 = self.bn4(self.conv4(x))

        return F.relu(h1 + h2)

class BottleNeckB(chainer.Chain):

    def __init__(self, in_size, ch):
        super(BottleNeckB, self).__init__()
        initialW = initializers.HeNormal()

        with self.init_scope():
            self.conv1 = L.Convolution2D(
                in_size, ch, 1, 1, 0, initialW=initialW, nobias=True)
            self.bn1 = L.BatchNormalization(ch)
            self.conv2 = L.Convolution2D(
                ch, ch, 3, 1, 1, initialW=initialW, nobias=True)
            self.bn2 = L.BatchNormalization(ch)
            self.conv3 = L.Convolution2D(
                ch, in_size, 1, 1, 0, initialW=initialW, nobias=True)
            self.bn3 = L.BatchNormalization(in_size)

    def __call__(self, x):
        h = F.relu(self.bn1(self.conv1(x)))
        h = F.relu(self.bn2(self.conv2(h)))
        h = self.bn3(self.conv3(h))

        return F.relu(h + x)

class Block(chainer.Chain):

    def __init__(self, layer, in_size, ch, out_size, stride=2):
        super(Block, self).__init__()
        self.add_link('a', BottleNeckA(in_size, ch, out_size, stride))
        for i in range(1, layer):
            self.add_link('b{}'.format(i), BottleNeckB(out_size, ch))
        self.layer = layer

    def __call__(self, x):
        h = self.a(x)
        for i in range(1, self.layer):
            h = self['b{}'.format(i)](h)

        return h


class ResRoIHead(chainer.Chain):

    """Faster R-CNN Head for VGG-16 based implementation.

    This class is used as a head for Faster R-CNN.
    This outputs class-wise classification based on feature
    maps in the given RoIs.

    Args:
        n_class (int): The number of classes possibly including the background.
        roi_size (int): Height and width of the feature maps after RoI-pooling.
        spatial_scale (float): Scale of the roi is resized.
        vgg_initialW (callable): Initializer for the layers corresponding to
            the VGG-16 layers.
        score_initialW (callable): Initializer for the score head.

    """

    def __init__(self, n_class, roi_size, spatial_scale,
                 fc_initialW=None, score_initialW=None, extract_len=None):
        # n_class includes the background
        super(ResRoIHead, self).__init__()
        if extract_len is None:
            extract_len = 1000
        with self.init_scope():
            self.res5 = Block(3, 1024, 512, 2048)
            self.fc = L.Linear(2048, extract_len, initialW=fc_initialW)
            self.score = L.Linear(extract_len, n_class, initialW=score_initialW)
        self.functions = collections.OrderedDict([
            ('res5',  [self.res5]),
            ("avg_pool", [functools.partial(F.average_pooling_2d, ksize=7, stride=1)]),  # because res5 will decrease height and width by factor of 2
            ("fc",    [self.fc]),
            ('relu',  [F.relu]),
            ("score", [self.score]),
        ])
        self.n_class = n_class
        self.roi_size = roi_size
        self.spatial_scale = spatial_scale  # ???????????????,????????????1/16.0
        self.activation = dict()


    def __call__(self, x, rois, roi_indices, layers=["res5"]):
        """Forward the chain.

        We assume that there are :math:`N` batches.

        Args:
            x (~chainer.Variable): 4D image variable.
            rois (array): A bounding box array containing coordinates of
                proposal boxes.  This is a concatenation of bounding box
                arrays from multiple images in the batch.
                Its shape is :math:`(R', 4)`. Given :math:`R_i` proposed
                RoIs from the :math:`i` th image,
                :math:`R' = \\sum _{i=1} ^ N R_i`.
            roi_indices (array): An array containing indices of images to
                which bounding boxes correspond to. Its shape is :math:`(R',)`.

        """
        target_layers = set(layers)
        roi_indices = roi_indices.astype(np.float32)
        indices_and_rois = self.xp.concatenate(
            (roi_indices[:, None], rois), axis=1)  # None means np.newaxis, concat along column
        pool = _roi_pooling_2d_yx(
            x, indices_and_rois, self.roi_size, self.roi_size,
            self.spatial_scale)
        h = pool
        for key, funcs in self.functions.items():
            for func in funcs:
                h = func(h)
            if key in target_layers:
                self.activation[key] = h
                target_layers.remove(key)
        return h


class ResnetFeatureExtractor(chainer.Chain):

    """Truncated VGG-16 that extracts a conv5_3 feature map.

    Args:
        initialW (callable): Initializer for the weights.

    """

    def __init__(self, fix):
        super(ResnetFeatureExtractor, self).__init__()
        with self.init_scope():
            self.conv1 = L.Convolution2D(3, 64, 7, 2, 3, initialW=initializers.HeNormal(), nobias=True)
            self.bn1 = L.BatchNormalization(64)
            self.res2 = Block(3, 64, 64, 256, 1)
            self.res3 = Block(4, 256, 128, 512)
            self.res4 = Block(23, 512, 256, 1024)
        self.fix = fix

    def __call__(self, x):

        if self.fix:
            with chainer.no_backprop_mode():
                h = self.bn1(self.conv1(x))
                h = F.max_pooling_2d(F.relu(h), 3, stride=2)
                h = self.res2(h)
        else:
            h = self.bn1(self.conv1(x))
            h = F.max_pooling_2d(F.relu(h), 3, stride=2)
            h = self.res2(h)
        h = self.res3(h)
        h = self.res4(h)
        return h


def _roi_pooling_2d_yx(x, indices_and_rois, outh, outw, spatial_scale):

    xy_indices_and_rois = indices_and_rois[:, [0, 2, 1, 4, 3]]
    pool = F.roi_pooling_2d(
        x, xy_indices_and_rois, outh, outw, spatial_scale)
    return pool


def _max_pooling_2d(x):
    return F.max_pooling_2d(x, ksize=2)
