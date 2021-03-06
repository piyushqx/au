import numpy as np

from chainer import cuda

from time_axis_rcnn.model.time_segment_network.util.bbox.bbox_util import encode_segment_target
from time_axis_rcnn.model.time_segment_network.util.bbox.bbox_util import segments_iou


class ProposalTargetCreator(object):
    """Assign ground truth bounding boxes to given RoIs.

    The :meth:`__call__` of this class generates training targets
    for each object proposal.
    This is used to train Faster RCNN [#]_.

    .. [#] Shaoqing Ren, Kaiming He, Ross Girshick, Jian Sun. \
    Faster R-CNN: Towards Real-Time Object Detection with \
    Region Proposal Networks. NIPS 2015.

    Args:
        n_sample (int): The number of sampled regions.
        pos_ratio (float): Fraction of regions that is labeled as a
            foreground.
        pos_iou_thresh (float): IoU threshold for a RoI to be considered as a
            foreground.
        neg_iou_thresh_hi (float): RoI is considered to be the background
            if IoU is in
            [:obj:`neg_iou_thresh_hi`, :obj:`neg_iou_thresh_hi`).
        neg_iou_thresh_lo (float): See above.

    """

    def __init__(self,
                 n_sample=128,
                 pos_ratio=0.25, pos_iou_thresh=0.5,
                 neg_iou_thresh_hi=0.5, neg_iou_thresh_lo=0.0
                 ):
        self.n_sample = n_sample
        self.pos_ratio = pos_ratio
        self.pos_iou_thresh = pos_iou_thresh
        self.neg_iou_thresh_hi = neg_iou_thresh_hi
        self.neg_iou_thresh_lo = neg_iou_thresh_lo

    def __call__(self, rois, roi_indices, gt_segments, labels, batch_seg_info,
                 loc_normalize_mean=(0., 0.),
                 loc_normalize_std=(0.1,  0.2)):
        """Assigns ground truth to sampled proposals.

        This function samples total of :obj:`self.n_sample` RoIs
        from the combination of :obj:`roi` and :obj:`bbox`.
        The RoIs are assigned with the ground truth class labels as well as
        bounding box offsets and scales to match the ground truth bounding
        boxes. As many as :obj:`pos_ratio * self.n_sample` RoIs are
        sampled as foregrounds.

        Offsets and scales of bounding boxes are calculated using
        :func:`chainercv.links.model.faster_rcnn.bbox2loc`.
        Also, types of input arrays and output arrays are same.

        Here are notations.

        * :math:`S` is the total number of sampled RoIs, which equals \
            :obj:`self.n_sample`.
        * :math:`L` is number of object classes possibly including the \
            background.

        Args:
            rois (np.array): Region of Interests (RoIs) from which we sample.
                Its shape is :math:`(R, 2)` `R` is across all images. each column is (batch_index, x_min, x_max)
            roi_indices (np.array): batch index of all RoIs shape = (R,)
            gt_segments (np.array): The coordinates of ground truth bounding segments.
                Its shape is :math:`(B, R', 2)`.
            labels (np.array): Ground truth bounding box labels. Its shape
                is :math:` (B, R')`.
            batch_seg_info (np.array) its shape is `(B, 2)`, which indicate AU group index, segment count of each batch index
            loc_normalize_mean (tuple of 2 floats): Mean values to normalize
                coordinates of bouding segments.
            loc_normalize_std (tupler of 2 floats): Standard deviation of
                the coordinates of bounding boxes.

        Returns:
            (array, array, array):

            * **sample_roi**: Regions of interests that are sampled. \
                Its shape is :math:`(S, 4)`.
            * **gt_roi_loc**: Offsets and scales to match \
                the sampled RoIs to the ground truth bounding boxes. \
                Its shape is :math:`(S, 4)`.
            * **gt_roi_label**: Labels assigned to sampled RoIs. Its shape is \
                :math:`(S,)`. Its range is :math:`[0, L]`. The label with \
                value 0 is the background.

        """
        xp = cuda.get_array_module(rois)
        rois = cuda.to_cpu(rois)
        roi_indices = cuda.to_cpu(roi_indices)
        gt_segments = cuda.to_cpu(gt_segments)
        labels = cuda.to_cpu(labels) # shape = (B, R')
        batch_seg_info = cuda.to_cpu(batch_seg_info)
        assert batch_seg_info.ndim == 2
        mini_batch = gt_segments.shape[0]
        assert mini_batch == labels.shape[0]
        batch_rois =[]
        batch_rois_indices = []
        batch_labels = []
        batch_roi_locs = []
        for b_id in range(mini_batch):
            _, seg_number = batch_seg_info[b_id]
            gt_seg = gt_segments[b_id][:int(seg_number)]
            roi_inds = np.where(roi_indices == b_id)

            all_rois = rois[roi_inds]
            label = labels[b_id][:int(seg_number)]  # shape = R,

            n_bbox, _ = gt_seg.shape
            all_rois = np.concatenate((all_rois, gt_seg), axis=0)  # (R + R', 2), ???gt box???????????????
            roi_indice_this_timeline = np.ones(all_rois.shape[0], dtype=np.int32) * b_id
            pos_roi_per_timeline = np.round(self.n_sample * self.pos_ratio)  # ??????????????????????????????
            iou = segments_iou(all_rois, gt_seg) # ????????????n x k??????????????????????????????n???rois???k???bbox???IOU
            gt_assignment = iou.argmax(axis=1) # shape = n, ????????????????????????????????????index???gt_assigment???shape???roi?????????????????????gt_assigment???????????????bbox???R'???
            max_iou = iou.max(axis=1) # shape = n, ???????????????????????????????????????????????????gt???bbox??????roi_bbox??????????????????????????????
            gt_roi_label = label[gt_assignment] + 1  # shape = (n, 12) ??????label???index??????bbox?????????????????????roi????????????????????????gt_assignment = iou.argmax(axis=1)
            # Select foreground RoIs as those with >= pos_iou_thresh IoU. IoU???????????????????????????ROI??????roi_bbox??????????????????
            pos_index = np.where(max_iou >= self.pos_iou_thresh)[0]
            pos_roi_this_timeline = int(min(pos_roi_per_timeline, pos_index.size))  # ???1:3???pos???????????????pos_index??????????????????
            if pos_index.size > 0:
                pos_index = np.random.choice(
                    pos_index, size=pos_roi_this_timeline, replace=False)  # ????????????

            # Select background RoIs as those within
            # [neg_iou_thresh_lo, neg_iou_thresh_hi). ?????????????????????????????????IoU????????????label
            neg_index = np.where((max_iou < self.neg_iou_thresh_hi) &
                                 (max_iou >= self.neg_iou_thresh_lo))[0]
            neg_roi_per_this_timeline = self.n_sample - pos_roi_this_timeline
            neg_roi_per_this_timeline = int(min(neg_roi_per_this_timeline,
                                             neg_index.size))
            if neg_index.size > 0:
                neg_index = np.random.choice(
                    neg_index, size=neg_roi_per_this_timeline, replace=False)

            # The indices that we're selecting (both positive and negative).
            keep_index = np.append(pos_index, neg_index)
            gt_roi_label = gt_roi_label[keep_index]
            # ????????????multi label?????????
            gt_roi_label[pos_roi_this_timeline:] = 0  # ?????????concat???????????????????????????????????????????????????????????????label???negative labels --> 0
            sample_roi = all_rois[keep_index]
            sample_roi_indices = roi_indice_this_timeline[keep_index]

            # Compute offsets and scales to match sampled RoIs to the GTs.
            gt_roi_loc = encode_segment_target(sample_roi, gt_seg[gt_assignment[keep_index]])  # shape = N, 2
            assert gt_roi_loc.shape[1] == 2, gt_roi_loc.shape
            gt_roi_loc = ((gt_roi_loc - np.array(loc_normalize_mean, np.float32)
                           ) / np.array(loc_normalize_std, np.float32))

            if xp != np:
                sample_roi = cuda.to_gpu(sample_roi)  # S,2
                gt_roi_loc = cuda.to_gpu(gt_roi_loc)  # S,2
                sample_roi_indices = cuda.to_gpu(sample_roi_indices) # S,
                gt_roi_label = cuda.to_gpu(gt_roi_label)  # S
                assert sample_roi.shape[0] == gt_roi_loc.shape[0] == sample_roi_indices.shape[0] == gt_roi_label.shape[0]
            batch_rois.append(sample_roi)
            batch_rois_indices.append(sample_roi_indices)
            batch_labels.append(gt_roi_label)
            batch_roi_locs.append(gt_roi_loc)
            # TODO _get_bbox_regression_labels???
        # return (B*S, 2)  (B*S,),  (B*S, 2),  (B*S, )
        return xp.concatenate(batch_rois, axis=0), xp.concatenate(batch_rois_indices, axis=0), \
               xp.concatenate(batch_roi_locs,axis=0), xp.concatenate(batch_labels, axis=0)
