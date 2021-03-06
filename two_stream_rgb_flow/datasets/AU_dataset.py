import cv2
import random

import chainer
import numpy as np
import os
from collections import defaultdict, OrderedDict

import config
from dataset_toolkit.compress_utils import get_zip_ROI_AU, get_AU_couple_child
from img_toolkit.face_mask_cropper import FaceMaskCropper
from two_stream_rgb_flow.constants.enum_type import TwoStreamMode

# obtain the cropped face image and bounding box and ground truth label for each box
class AUDataset(chainer.dataset.DatasetMixin):

    def __init__(self, database, L, fold, split_name, split_index, mc_manager, train_all_data,two_stream_mode,
                 pretrained_target="",paper_report_label_idx=None):
        self.database = database
        self.split_name = split_name
        self.L = L  # used for the optical flow image fetch at before L/2 and after L/2
        self.au_couple_dict = get_zip_ROI_AU()
        self.mc_manager = mc_manager
        self.au_couple_child_dict = get_AU_couple_child(self.au_couple_dict)
        self.AU_intensity_label = {}  # subject + "/" + emotion_seq + "/" + frame => ... not implemented
        self.pretrained_target = pretrained_target
        self.two_stream_mode = two_stream_mode
        self.dir = config.DATA_PATH[database] # BP4D/DISFA/ BP4D_DISFA
        self.paper_report_label_idx = paper_report_label_idx
        if train_all_data:
            id_list_file_path = os.path.join(self.dir + "/idx/{}_fold".format(fold),
                                             "full_pretrain.txt")
        else:
            id_list_file_path = os.path.join(self.dir + "/idx/{0}_fold".format(fold),
                                             "id_{0}_{1}.txt".format(split_name, split_index))
        self.result_data = []

        print("idfile:{}".format(id_list_file_path))
        with open(id_list_file_path, "r") as file_obj:
            for idx, line in enumerate(file_obj):
                if line.rstrip():
                    line = line.rstrip()
                    relative_path, au_set_str, _, current_database_name = line.split("\t")
                    AU_set = set(AU for AU in au_set_str.split(',') if AU in config.AU_ROI and AU in config.AU_SQUEEZE.inv)
                    if au_set_str == "0":
                        AU_set = set()
                    rgb_path = config.RGB_PATH[current_database_name] + os.path.sep + relative_path  # id file ???????????????
                    flow_path = config.FLOW_PATH[current_database_name] + os.path.sep + relative_path
                    if os.path.exists(rgb_path):
                        self.result_data.append((rgb_path, flow_path, AU_set, current_database_name))

        self.result_data.sort(key=lambda entry: (entry[0].split("/")[-3],entry[0].split("/")[-2],
                                                 int(entry[0].split("/")[-1][:entry[0].split("/")[-1].rindex(".")])))
        self._num_examples = len(self.result_data)
        print("read id file done, all examples:{}".format(self._num_examples))

    def __len__(self):
        return self._num_examples

    def collect_flow_image_paths(self, data_index):
        candidate = self.result_data[max(data_index - self.L//2,0) : min(data_index + self.L//2, len(self))]
        seq_id = self.extract_sequence_key(self.result_data[data_index][0])
        collect_flow_path = []
        for rgb_path, flow_path, _, _ in candidate:
            other_seq_id = self.extract_sequence_key(rgb_path)
            if other_seq_id == seq_id:
                collect_flow_path.append({"flow":flow_path, "rgb":rgb_path})
        return collect_flow_path

    def extract_sequence_key(self, img_path):
        return "/".join((img_path.split("/")[-3], img_path.split("/")[-2]))

    def assign_label(self, couple_box_dict, current_AU_couple, bbox, label):
        AU_couple_bin = dict()
        for au_couple_tuple, _ in couple_box_dict.items():
            # use connectivity components to seperate polygon
            AU_inside_box_set = current_AU_couple[au_couple_tuple]

            AU_bin = np.zeros(shape=len(config.AU_SQUEEZE), dtype=np.int32)  # ???0?????????????????????????????????
            for AU in AU_inside_box_set:  # AU_inside_box_set may has -3 or ?3
                if AU not in config.AU_SQUEEZE.inv:
                    continue
                AU_squeeze = config.AU_SQUEEZE.inv[AU]  # AU_squeeze type = int
                np.put(AU_bin, AU_squeeze, 1)
            AU_couple_bin[au_couple_tuple] = AU_bin  # for the child
        # ??????????????????????????????child_AU_couple
        for au_couple_tuple, box_list in couple_box_dict.items():
            AU_child_bin = np.zeros(shape=len(config.AU_SQUEEZE), dtype=np.int32)
            if au_couple_tuple in self.au_couple_child_dict:
                for au_couple_child in self.au_couple_child_dict[au_couple_tuple]:
                    AU_child_bin = np.bitwise_or(AU_child_bin, AU_couple_bin[au_couple_child])
            AU_bin_tmp = AU_couple_bin[au_couple_tuple]  # ???0?????????????????????????????????
            AU_bin = np.bitwise_or(AU_child_bin, AU_bin_tmp)
            bbox.extend(box_list)
            for _ in box_list:
                label.append(AU_bin)

    def get_example(self, i):
        '''
        Returns a color image and bounding boxes. The image is in CHW format.
        The returned image is RGB.

        :param i:  the index of the example
        :return: tuple of an image and its all bounding box
        '''
        if i > len(self.result_data):
            raise IndexError("Index too large")
        rgb_path, _, AU_set, database = self.result_data[i]
        flow_path_list = []
        if self.two_stream_mode == TwoStreamMode.optical_flow or self.two_stream_mode == TwoStreamMode.rgb_flow:
            flow_path_list = self.collect_flow_image_paths(i)
        elif self.two_stream_mode == TwoStreamMode.rgb:
            flow_path_list = [{"rgb":rgb_path, "flow":rgb_path}]
        key_prefix = self.database + "@512|"
        if self.pretrained_target is not None and len(self.pretrained_target) > 0:
            key_prefix = self.pretrained_target + "@512|"

        flow_face_list = []
        for flow_dict in flow_path_list:
            adjacent_rgb_path = flow_dict["rgb"]
            adjacent_flow_path = flow_dict["flow"]
            try:
                # FIXME read too slow, use the same rgb path to accelerate speed . but this trick is not accurate
                flow_face, _ = FaceMaskCropper.get_cropface_and_box(adjacent_flow_path, adjacent_rgb_path,
                                                                             channel_first=True,
                                                                             mc_manager=self.mc_manager,
                                                                             key_prefix=key_prefix)
                flow_face = flow_face[:2, :, :]  # 2, H, W
                flow_face_list.append(flow_face) # only use two channel x and y of optical flow image
            except IndexError:
                print("image path : {} not get box".format(adjacent_rgb_path))
                flow_face = cv2.imread(adjacent_flow_path)
                flow_face = cv2.resize(flow_face, config.IMG_SIZE)
                flow_face = np.transpose(flow_face, axes=(2,0,1))
                flow_face = flow_face[:2, :, :]
                flow_face_list.append(flow_face)

        flow_face_list = np.stack(flow_face_list)  # T, C, H, W
        if len(flow_face_list) < self.L:
            rest_pad_len = self.L - len(flow_face_list)
            flow_face_list = np.pad(flow_face_list, ((0, rest_pad_len), (0,0), (0,0), (0,0)), 'mean')
        assert flow_face_list.shape[0]==self.L

        try:
            # print("begin fetch cropped image and bbox {}".format(img_path))
            rgb_face, AU_box_dict = FaceMaskCropper.get_cropface_and_box(rgb_path, rgb_path,
                                                                         channel_first=True,
                                                                         mc_manager=self.mc_manager,
                                                                         key_prefix=key_prefix)
        except IndexError:
            print("image path : {} not get box".format(rgb_path))
            label = np.zeros(len(config.AU_SQUEEZE), dtype=np.int32)
            for AU in AU_set:
                np.put(label, config.AU_SQUEEZE.inv[AU], 1)
            if self.paper_report_label_idx:
                label = label[self.paper_report_label_idx]

            rgb_face = np.transpose(cv2.resize(cv2.imread(rgb_path), config.IMG_SIZE), (2, 0, 1))

            whole_bbox = np.tile(np.array([1, 1, config.IMG_SIZE[1] - 1, config.IMG_SIZE[0] - 1], dtype=np.float32),
                                 (config.BOX_NUM[database], 1))
            whole_label = np.tile(label, (config.BOX_NUM[database], 1))
            return rgb_face, flow_face_list, whole_bbox, whole_label



        current_AU_couple = defaultdict(set)  # key = AU couple, value = AU ????????????????????????????????????AU
        couple_box_dict = OrderedDict()  # key= AU couple

        # mask_path_dict's key AU maybe 3 or -2 or ?5
        for AU in AU_set:
            _AU = AU if AU.isdigit() else AU[1:]
            # print("AU:",AU,"_AU:",_AU)
            try:
                current_AU_couple[self.au_couple_dict[_AU]].add(
                    AU)  # value list may contain ?2 or -1, ?????????????????????????????????????????????AU?????????
            except KeyError:
                print(list(self.au_couple_dict.keys()), _AU)
                raise
        for AU, box_list in sorted(AU_box_dict.items(), key=lambda e: int(e[0])):
            _AU = AU if AU.isdigit() else AU[1:]
            if _AU in config.SYMMETRIC_AU and len(box_list) == 1:
                box_list.append(random.choice(box_list))
            couple_box_dict[self.au_couple_dict[_AU]] = box_list  # ?????????????????????????????????????????????AU?????????
        label = []  # one box may have multiple labels. so each entry is 10101110 binary code
        bbox = []  # AU = 0?????????box???????????????
        self.assign_label(couple_box_dict, current_AU_couple, bbox, label)
        # print("assigned label over")
        assert len(bbox) > 0
        bbox = np.stack(bbox).astype(np.float32)
        label = np.stack(label).astype(np.int32)
        # bbox, label = self.proposal(bbox, label)  # ??????????????????batch?????????box????????????
        assert bbox.shape[0] == label.shape[0]

        if bbox.shape[0] != config.BOX_NUM[database]:
            print("found one error image: {0} box_number:{1}".format(rgb_path, bbox.shape[0]))
            bbox = bbox.tolist()
            label = label.tolist()

            if len(bbox) > config.BOX_NUM[database]:
                all_del_idx = []
                for idx, box in enumerate(bbox):
                    if FaceMaskCropper.calculate_area(*box) / float(config.IMG_SIZE[0] * config.IMG_SIZE[1]) < 0.01:
                        all_del_idx.append(idx)
                for del_idx in all_del_idx:
                    del bbox[del_idx]
                    del label[del_idx]

            while len(bbox) < config.BOX_NUM[database]:
                index = 0
                bbox.insert(0, bbox[index])
                label.insert(0, label[index])
            while len(bbox) > config.BOX_NUM[database]:
                del bbox[-1]
                del label[-1]

            bbox = np.stack(bbox)
            label = np.stack(label)

        if self.paper_report_label_idx is not None:
            label = label[:, self.paper_report_label_idx]
        return rgb_face, flow_face_list, bbox, label
