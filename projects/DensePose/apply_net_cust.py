#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.

import argparse
import glob
import logging
import os
from re import X
import sys
from typing import Any, ClassVar, Dict, List
import torch
import cv2
import subprocess
import numpy as np

from detectron2.config import CfgNode, get_cfg
from detectron2.data.detection_utils import read_image
from detectron2.engine.defaults import DefaultPredictor
from detectron2.structures.instances import Instances
from detectron2.utils.logger import setup_logger

from densepose import add_densepose_config
from densepose.structures import DensePoseChartPredictorOutput, DensePoseEmbeddingPredictorOutput
from densepose.utils.logger import verbosity_to_level
from densepose.vis.base import CompoundVisualizer
from densepose.vis.bounding_box import ScoredBoundingBoxVisualizer
from densepose.vis.densepose_outputs_vertex import (
    DensePoseOutputsTextureVisualizer,
    DensePoseOutputsVertexVisualizer,
    get_texture_atlases,
)
from densepose.vis.densepose_results import (
    DensePoseResultsContourVisualizer,
    DensePoseResultsFineSegmentationVisualizer,
    DensePoseResultsUVisualizer,
    DensePoseResultsVVisualizer,
)
from densepose.vis.densepose_results_textures import (
    DensePoseResultsVisualizerWithTexture,
    get_texture_atlas,
)
from densepose.vis.extractor import (
    CompoundExtractor,
    DensePoseOutputsExtractor,
    DensePoseResultExtractor,
    create_extractor,
)

DOC = """Apply Net - a tool to print / visualize DensePose results
"""

LOGGER_NAME = "apply_net"
logger = logging.getLogger(LOGGER_NAME)

_ACTION_REGISTRY: Dict[str, "Action"] = {}


class Action(object):
    @classmethod
    def add_arguments(cls: type, parser: argparse.ArgumentParser):
        parser.add_argument(
            "-v",
            "--verbosity",
            action="count",
            help="Verbose mode. Multiple -v options increase the verbosity.",
        )


def register_action(cls: type):
    """
    Decorator for action classes to automate action registration
    """
    global _ACTION_REGISTRY
    _ACTION_REGISTRY[cls.COMMAND] = cls
    return cls


class InferenceAction(Action):
    @classmethod
    def add_arguments(cls: type, parser: argparse.ArgumentParser):
        super(InferenceAction, cls).add_arguments(parser)
        parser.add_argument("cfg", metavar="<config>", help="Config file")
        parser.add_argument("model", metavar="<model>", help="Model file")
        parser.add_argument("input", metavar="<input>", help="Input data")
        parser.add_argument(
            "--opts",
            help="Modify config options using the command-line 'KEY VALUE' pairs",
            default=[],
            nargs=argparse.REMAINDER,
        )

    @classmethod
    def execute(cls: type, args: argparse.Namespace):
        logger.info(f"Loading config from {args.cfg}")
        opts = []
        cfg = cls.setup_config(args.cfg, args.model, args, opts)
        logger.info(f"Loading model from {args.model}")
        predictor = DefaultPredictor(cfg)
        logger.info(f"Loading data from {args.input}")
        file_list = cls._get_input_file_list(args.input)
        if len(file_list) == 0:
            logger.warning(f"No input images for {args.input}")
            return
        context = cls.create_context(args, cfg)
        for file_name in file_list:
            img = read_image(file_name, format="BGR")  # predictor expects BGR image.
            with torch.no_grad():
                outputs = predictor(img)["instances"]
                cls.execute_on_outputs(context, {"file_name": file_name, "image": img}, outputs)
        cls.postexecute(context)

    @classmethod
    def setup_config(
            cls: type, config_fpath: str, model_fpath: str, args: argparse.Namespace, opts: List[str]
    ):
        cfg = get_cfg()
        add_densepose_config(cfg)
        cfg.merge_from_file(config_fpath)
        cfg.merge_from_list(args.opts)
        if opts:
            cfg.merge_from_list(opts)
        cfg.MODEL.WEIGHTS = model_fpath
        cfg.freeze()
        return cfg

    @classmethod
    def _get_input_file_list(cls: type, input_spec: str):
        if os.path.isdir(input_spec):
            file_list = [
                os.path.join(input_spec, fname)
                for fname in os.listdir(input_spec)
                if os.path.isfile(os.path.join(input_spec, fname))
            ]
        elif os.path.isfile(input_spec):
            file_list = [input_spec]
        else:
            file_list = glob.glob(input_spec)
        return file_list


@register_action
class DumpAction(InferenceAction):
    """
    Dump action that outputs results to a pickle file
    """

    COMMAND: ClassVar[str] = "dump"

    @classmethod
    def add_parser(cls: type, subparsers: argparse._SubParsersAction):
        parser = subparsers.add_parser(cls.COMMAND, help="Dump model outputs to a file.")
        cls.add_arguments(parser)
        parser.set_defaults(func=cls.execute)

    @classmethod
    def add_arguments(cls: type, parser: argparse.ArgumentParser):
        super(DumpAction, cls).add_arguments(parser)
        parser.add_argument(
            "--output",
            metavar="<dump_file>",
            default="results.pkl",
            help="File name to save dump to",
        )

    @classmethod
    def execute_on_outputs(
            cls: type, context: Dict[str, Any], entry: Dict[str, Any], outputs: Instances
    ):
        image_fpath = entry["file_name"]
        logger.info(f"Processing {image_fpath}")
        result = {"file_name": image_fpath}
        if outputs.has("scores"):
            result["scores"] = outputs.get("scores").cpu()
        if outputs.has("pred_boxes"):
            result["pred_boxes_XYXY"] = outputs.get("pred_boxes").tensor.cpu()
            if outputs.has("pred_densepose"):
                if isinstance(outputs.pred_densepose, DensePoseChartPredictorOutput):
                    extractor = DensePoseResultExtractor()
                elif isinstance(outputs.pred_densepose, DensePoseEmbeddingPredictorOutput):
                    extractor = DensePoseOutputsExtractor()
                result["pred_densepose"] = extractor(outputs)[0]
        context["results"].append(result)

    @classmethod
    def create_context(cls: type, args: argparse.Namespace, cfg: CfgNode):
        context = {"results": [], "out_fname": args.output}
        return context

    @classmethod
    def postexecute(cls: type, context: Dict[str, Any]):
        out_fname = context["out_fname"]
        out_dir = os.path.dirname(out_fname)
        if len(out_dir) > 0 and not os.path.exists(out_dir):
            os.makedirs(out_dir)
        with open(out_fname, "wb") as hFile:
            torch.save(context["results"], hFile)
            logger.info(f"Output saved to {out_fname}")


@register_action
class ShowAction(InferenceAction):
    """
    Show action that visualizes selected entries on an image
    """

    COMMAND: ClassVar[str] = "show"
    VISUALIZERS: ClassVar[Dict[str, object]] = {
        "dp_contour": DensePoseResultsContourVisualizer,
        "dp_segm": DensePoseResultsFineSegmentationVisualizer,
        "dp_u": DensePoseResultsUVisualizer,
        "dp_v": DensePoseResultsVVisualizer,
        "dp_iuv_texture": DensePoseResultsVisualizerWithTexture,
        "dp_cse_texture": DensePoseOutputsTextureVisualizer,
        "dp_vertex": DensePoseOutputsVertexVisualizer,
        "bbox": ScoredBoundingBoxVisualizer,
    }

    @classmethod
    def add_parser(cls: type, subparsers: argparse._SubParsersAction):
        parser = subparsers.add_parser(cls.COMMAND, help="Visualize selected entries")
        cls.add_arguments(parser)
        parser.set_defaults(func=cls.execute)

    @classmethod
    def add_arguments(cls: type, parser: argparse.ArgumentParser):
        super(ShowAction, cls).add_arguments(parser)
        parser.add_argument(
            "visualizations",
            metavar="<visualizations>",
            help="Comma separated list of visualizations, possible values: "
                 "[{}]".format(",".join(sorted(cls.VISUALIZERS.keys()))),
        )
        parser.add_argument(
            "--min_score",
            metavar="<score>",
            default=0.8,
            type=float,
            help="Minimum detection score to visualize",
        )
        parser.add_argument(
            "--nms_thresh", metavar="<threshold>", default=None, type=float, help="NMS threshold"
        )
        parser.add_argument(
            "--texture_atlas",
            metavar="<texture_atlas>",
            default=None,
            help="Texture atlas file (for IUV texture transfer)",
        )
        parser.add_argument(
            "--texture_atlases_map",
            metavar="<texture_atlases_map>",
            default=None,
            help="JSON string of a dict containing texture atlas files for each mesh",
        )
        parser.add_argument(
            "--output",
            metavar="<image_file>",
            default="outputres.png",
            help="File name to save output to",
        )

    @classmethod
    def setup_config(
            cls: type, config_fpath: str, model_fpath: str, args: argparse.Namespace, opts: List[str]
    ):
        opts.append("MODEL.ROI_HEADS.SCORE_THRESH_TEST")
        opts.append(str(args.min_score))
        if args.nms_thresh is not None:
            opts.append("MODEL.ROI_HEADS.NMS_THRESH_TEST")
            opts.append(str(args.nms_thresh))
        cfg = super(ShowAction, cls).setup_config(config_fpath, model_fpath, args, opts)
        return cfg

    @classmethod
    def generate_vector(cls, file_in, file_out):
        cmd = "autotrace -filter-iterations=4 -input-format bmp -output-format svg -output-file " + file_out + " " + file_in
        subprocess.Popen(cmd, shell=True, executable='/bin/bash')

    @classmethod
    def double_image(cls, image):
        scale_percent = 200  # percent of original size
        width = int(image.shape[1] * scale_percent / 100)
        height = int(image.shape[0] * scale_percent / 100)
        dim = (width, height)

        # resize image
        image = cv2.resize(image, dim, interpolation=cv2.INTER_AREA)
        return image

    @classmethod
    def find_body_part(cls, body_parts, body_part_index):
        arr = body_parts.copy()
        arr[arr != body_part_index] = 0
        arr[arr == body_part_index] = 255

        contours, hierarchy = cv2.findContours(arr, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
        return contours

    @classmethod
    def generate_bitmaps(cls, prefix, image, output_orig, outline):
        blank = np.zeros(shape=image.shape, dtype=np.uint8)
        blank_outline = np.zeros(shape=image.shape, dtype=np.uint8)
        solid_outline = np.zeros(shape=image.shape, dtype=np.uint8)

        # Individual body parts
        for i in range(1, 24):
            #contours = cls.find_body_part(orig, i)
            contours = cls.find_body_part(output_orig, i)

            if len(contours) > 0:
                cv2.drawContours(image, contours, -1, (0, 0, 0), 2)
                cv2.drawContours(blank, contours, -1, (255, 255, 255), 2)

        # Outline all parts    
        blank = cv2.bitwise_not(blank)
        blank = cls.double_image(blank)
        cv2.imwrite("/content/" + prefix + "blank.bmp", blank)
        cls.generate_vector("/content/" + prefix + "blank.bmp", "/content/" + prefix + "blank.svg")

        # Original image
        image = cls.double_image(image)
        cv2.imwrite("/content/" + prefix + "image.bmp", image)
        cls.generate_vector("/content/" + prefix + "image.bmp", "/content/" + prefix + "image.svg")

        # Gray scale image	
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        cv2.imwrite("/content/" + prefix + "image_bw.bmp", gray)
        cls.generate_vector("/content/" + prefix + "image_bw.bmp", "/content/" + prefix + "image_bw.svg")

        # All in one outline
        contours_o = cls.find_body_part(outline, 1)
        if len(contours_o) > 0:
            cv2.drawContours(blank_outline, contours_o, -1, (255, 255, 255), 2)
            cv2.drawContours(solid_outline, contours_o, -1, (255, 255, 255), -1)

        blank_outline = cv2.bitwise_not(blank_outline)
        blank_outline = cls.double_image(blank_outline)
        cv2.imwrite("/content/" + prefix + "blank_outline.bmp", blank_outline)
        cls.generate_vector("/content/" + prefix + "blank_outline.bmp", "/content/" + prefix + "blank_outline.svg")

        solid_outline = cv2.bitwise_not(solid_outline)
        solid_outline = cls.double_image(solid_outline)
        cv2.imwrite("/content/" + prefix + "solid_outline.bmp", solid_outline)
        cls.generate_vector("/content/" + prefix + "solid_outline.bmp", "/content/" + prefix + "solid_outline.svg")

    @classmethod
    def execute_on_outputs(
            cls: type, context: Dict[str, Any], entry: Dict[str, Any], outputs: Instances
    ):
        import cv2
        import numpy as np
        import os

        visualizer = context["visualizer"]
        extractor = context["extractor"]
        image_fpath = entry["file_name"]

        _,prefix = os.path.split(image_fpath)
        prefix,_ = os.path.splitext(prefix)
        prefix = "/results/" + prefix + "_"
        print(prefix)
        if not os.path.exists("/content/results"):
          os.mkdir("/content/results")



        logger.info(f"Processing {image_fpath}")
        image = cv2.cvtColor(entry["image"], cv2.COLOR_BGR2GRAY)
        image = np.zeros(shape=image.shape, dtype=np.uint8)
        image = np.tile(image[:, :, np.newaxis], [1, 1, 3])

        data = extractor(outputs)
        # image_vis = visualizer.visualize(image, data)
        bbox = data[0][1].cpu().numpy()

        I = data[0][0][0].labels.cpu().numpy()
        I = I.astype(np.uint8)

        I[I == 2] = 1
        I[I == 17] = 15
        I[I == 18] = 16
        I[I == 7] = 9
        I[I == 8] = 10
        I[I == 11] = 13
        I[I == 12] = 14
        I[I == 19] = 21
        I[I == 20] = 22
        I[I == 24] = 23

        output_orig = np.copy(I)
        np.save("/content/output_orig.npy", output_orig)

        outline = np.copy(I)
        outline[outline > 0] = 1

        np.save("/content/output_outline.npy", outline)

        # 0      = Background
        # 1, 2   = Torso
        # 3      = Right Hand
        # 4      = Left Hand
        # 5      = Right Foot
        # 6      = Left Foot
        # 7, 9   = Upper Leg Right
        # 8, 10  = Upper Leg Left
        # 11, 13 = Lower Leg Right
        # 12, 14 = Lower Leg Left
        # 15, 17 = Upper Arm Left
        # 16, 18 = Upper Arm Right
        # 19, 21 = Lower Arm Left
        # 20, 22 = Lower Arm Right
        # 23, 24 = Head

        # print(np.where(I == 1))

        print(I.shape)

        I = I.astype(np.float32) * 10.625
        I = I.astype(np.uint8)

        CMAP = cv2.COLORMAP_PARULA  # Fave so far
        I = cv2.applyColorMap(I, CMAP)

        # Make sure background is black
        my_array = cv2.applyColorMap(np.asarray([[[0, 0, 0]]], dtype=np.uint8), CMAP)
        bg = my_array[0][0]

        I[np.all(I == bg, axis=-1)] = [255, 255, 255]

        x, y, w, h = bbox[0].astype(np.int32)
        # image_target_bgr = np.zeros(shape=image.shape, dtype=np.uint8)
        image_target_bgr = np.full(image.shape, 255, dtype=np.uint8)
        image_target_bgr[y:y + h, x:x + w] = I

        np.save("/content/" + prefix + "output.npy", I)

        cv2.imwrite("/content/results/mytest.jpg", image_target_bgr)

        # output_orig
        # outline
        # I

        cls.generate_bitmaps(prefix, I, output_orig, outline)

        #np.save("/content/output_outline.npy", outline)

        # entry_idx = context["entry_idx"] + 1
        # out_fname = cls._get_out_fname(entry_idx, context["out_fname"])
        # out_dir = os.path.dirname(out_fname)
        # if len(out_dir) > 0 and not os.path.exists(out_dir):
        #    os.makedirs(out_dir)
        # cv2.imwrite(out_fname, image_vis)
        # logger.info(f"Output saved to {out_fname}")
        context["entry_idx"] += 1

    @classmethod
    def postexecute(cls: type, context: Dict[str, Any]):
        pass

    @classmethod
    def _get_out_fname(cls: type, entry_idx: int, fname_base: str):
        base, ext = os.path.splitext(fname_base)
        return base + ".{0:04d}".format(entry_idx) + ext

    @classmethod
    def create_context(cls: type, args: argparse.Namespace, cfg: CfgNode) -> Dict[str, Any]:
        vis_specs = args.visualizations.split(",")
        visualizers = []
        extractors = []
        for vis_spec in vis_specs:
            texture_atlas = get_texture_atlas(args.texture_atlas)
            texture_atlases_dict = get_texture_atlases(args.texture_atlases_map)
            vis = cls.VISUALIZERS[vis_spec](
                cfg=cfg,
                texture_atlas=texture_atlas,
                texture_atlases_dict=texture_atlases_dict,
            )
            visualizers.append(vis)
            extractor = create_extractor(vis)
            extractors.append(extractor)
        visualizer = CompoundVisualizer(visualizers)
        extractor = CompoundExtractor(extractors)
        context = {
            "extractor": extractor,
            "visualizer": visualizer,
            "out_fname": args.output,
            "entry_idx": 0,
        }
        return context


def create_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=DOC,
        formatter_class=lambda prog: argparse.HelpFormatter(prog, max_help_position=120),
    )
    parser.set_defaults(func=lambda _: parser.print_help(sys.stdout))
    subparsers = parser.add_subparsers(title="Actions")
    for _, action in _ACTION_REGISTRY.items():
        action.add_parser(subparsers)
    return parser


def main():
    parser = create_argument_parser()
    args = parser.parse_args()
    verbosity = args.verbosity if hasattr(args, "verbosity") else None
    global logger
    logger = setup_logger(name=LOGGER_NAME)
    logger.setLevel(verbosity_to_level(verbosity))
    args.func(args)


if __name__ == "__main__":
    main()
