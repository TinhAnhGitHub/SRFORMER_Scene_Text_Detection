"""
Scene Text Detector Module

This module provides functionality for detecting and visualizing.
It utilizes the Detectron2 framework and custome ADET utilities
"""

import os
from typing import List, Dict, Any, Optional
import glob
import time
import cv2

from tqdm.notebook import tqdm as tqdm  
import numpy as np

import torch
import matplotlib.pyplot as plt
import multiprocessing as mp
import bisect
import atexit


from detectron2.utils.logger import setup_logger
from detectron2.data import MetadataCatalog
from detectron2.engine.defaults import DefaultPredictor
from detectron2.utils.video_visualizer import VideoVisualizer
from detectron2.utils.visualizer import ColorMode, Visualizer
from detectron2.config import CfgNode


from adet.config import get_cfg
from adet.utils.visualizer import TextVisualizer

class AsyncPredictor:
    """
    Asynchronous predictor that runs the model in parallel processes.
    """

    class _StopToken:
        pass

    class _PredictWorker(mp.Process):
        def __init__(self, cfg: CfgNode, task_queue: mp.Queue, result_queue: mp.Queue):
            self.cfg = cfg
            self.task_queue = task_queue
            self.result_queue = result_queue
            super().__init__()

        def run(self):
            predictor = DefaultPredictor(self.cfg)

            while True:
                task = self.task_queue.get()
                if isinstance(task, AsyncPredictor._StopToken):
                    break
                idx, data = task
                result = predictor(data)
                self.result_queue.put((idx, result))

    def __init__(self, cfg: CfgNode, num_gpus: int = 1):
        """
        Initialize the AsyncPredictor.

        Args:
            cfg (CfgNode): Configuration for the model.
            num_gpus (int): Number of GPUs to use.
        """
        num_workers = max(num_gpus, 1)
        self.task_queue = mp.Queue(maxsize=num_workers * 3)
        self.result_queue = mp.Queue(maxsize=num_workers * 3)
        self.procs = []
        for gpuid in range(max(num_gpus, 1)):
            cfg = cfg.clone()
            cfg.defrost()
            cfg.MODEL.DEVICE = f"cuda:{gpuid}" if num_gpus > 0 else "cpu"
            self.procs.append(
                AsyncPredictor._PredictWorker(cfg, self.task_queue, self.result_queue)
            )

        self.put_idx = 0
        self.get_idx = 0
        self.result_rank = []
        self.result_data = []

        for p in self.procs:
            p.start()
        atexit.register(self.shutdown)

    def put(self, image: np.ndarray) -> None:
        """
        Put an image into the task queue.

        Args:
            image (np.ndarray): Input image.
        """
        self.put_idx += 1
        self.task_queue.put((self.put_idx, image))

    def get(self) -> Any:
        """
        Get a result from the result queue.

        Returns:
            Any: Prediction result.
        """
        self.get_idx += 1
        if len(self.result_rank) and self.result_rank[0] == self.get_idx:
            res = self.result_data[0]
            del self.result_data[0], self.result_rank[0]
            return res

        while True:
            # Make sure the results are returned in the correct order
            idx, res = self.result_queue.get()
            if idx == self.get_idx:
                return res
            insert = bisect.bisect(self.result_rank, idx)
            self.result_rank.insert(insert, idx)
            self.result_data.insert(insert, res)

    def __len__(self) -> int:
        return self.put_idx - self.get_idx

    def __call__(self, image: np.ndarray) -> Any:
        """
        Process an image asynchronously.

        Args:
            image (np.ndarray): Input image.

        Returns:
            Any: Prediction result.
        """
        self.put(image)
        return self.get()

    def shutdown(self) -> None:
        """
        Shutdown all worker processes.
        """
        for _ in self.procs:
            self.task_queue.put(AsyncPredictor._StopToken())

    @property
    def default_buffer_size(self) -> int:
        return len(self.procs) * 5
    



class VisualizationDemo(object):
    def __init__(self, cfg, instance_mode=ColorMode.IMAGE, parallel=False):
        """
        Args:
            cfg (CfgNode):
            instance_mode (ColorMode):
            parallel (bool): whether to run the model in different processes from visualization.
                Useful since the visualization logic can be slow.
        """
        self.metadata = MetadataCatalog.get(
            cfg.DATASETS.TEST[0] if len(cfg.DATASETS.TEST) else "__unused"
        )
        self.cfg = cfg
        self.cpu_device = torch.device("cpu")
        self.instance_mode = instance_mode
        self.vis_text = cfg.MODEL.TRANSFORMER.ENABLED

        self.parallel = parallel
        if parallel:
            num_gpu = torch.cuda.device_count()
            self.predictor = AsyncPredictor(cfg, num_gpus=num_gpu)
        else:
            self.predictor = DefaultPredictor(cfg)

    def run_on_image(self, image):
        """
        Args:
            image (np.ndarray): an image of shape (H, W, C) (in BGR order).
                This is the format used by OpenCV.

        Returns:
            predictions (dict): the output of the model.
            vis_output (VisImage): the visualized image output.
        """
        vis_output = None
        predictions = self.predictor(image)
       
        image = image[:, :, ::-1]
        if self.vis_text:
            visualizer = TextVisualizer(image, self.metadata, instance_mode=self.instance_mode, cfg=self.cfg)
        else:
            visualizer = Visualizer(image, self.metadata, instance_mode=self.instance_mode)

        if "bases" in predictions:
            self.vis_bases(predictions["bases"])
        if "panoptic_seg" in predictions:
            panoptic_seg, segments_info = predictions["panoptic_seg"]
            vis_output = visualizer.draw_panoptic_seg_predictions(
                panoptic_seg.to(self.cpu_device), segments_info
            )
        else:
            if "sem_seg" in predictions:
                vis_output = visualizer.draw_sem_seg(
                    predictions["sem_seg"].argmax(dim=0).to(self.cpu_device))
            if "instances" in predictions:
                instances = predictions["instances"].to(self.cpu_device)
                vis_output = visualizer.draw_instance_predictions(predictions=instances)

        return predictions, vis_output

    
        

class SceneTextDetector:
    """
    A clas for detecting text in scene using a pre-trained model.

    This class provides methods for processing images and visualize the results of text detection in various scenes.
    """

    def __init__(self, model_weights: str, confidence_threshold: float = 0.5, config_file = './configs/SRFormer/TotalText/R_50_poly.yaml'):
        """Initilize the SceneTextDetector

        Args:
            config_file (str): Path to the configuration file
           
            confidence_threshold (float, optional): Confidence threshold for detections. Defaults to 0.5.
        """

        self.logger = setup_logger()
        self.cfg = self.setup_cfg(
            model_weights = model_weights,
            confidence_threshold= confidence_threshold,
            config_file= config_file
        )
        self.demo = VisualizationDemo(self.cfg)


    

    def setup_cfg(self, model_weights: str, confidence_threshold: float, config_file: str = './configs/SRFormer/TotalText/R_50_poly.yaml') -> CfgNode:
        """Set up the configuration for the model

        Args:
            config_file (str): Path to the configuration file
            model_weights (str): Pat to the model weights file
            confidence_threshold (float): Confidence threshold for detections

        Returns:
            CfgNode: Configuration object
        """
        cfg = get_cfg()
        cfg.merge_from_file(config_file)
        cfg.MODEL.WEIGHTS = model_weights
        cfg.MODEL.RETINANET.SCORE_THRESH_TEST = confidence_threshold
        cfg.MODEL.ROI_HEADS.SCORE_THRESH_TEST = confidence_threshold
        cfg.MODEL.FCOS.INFERENCE_TH_TEST = confidence_threshold
        cfg.MODEL.MEInst.INFERENCE_TH_TEST = confidence_threshold
        cfg.MODEL.PANOPTIC_FPN.COMBINE.INSTANCES_CONFIDENCE_THRESH = confidence_threshold
        cfg.freeze()
        return cfg
    
    def ctrl_pnt_to_poly(self, pnt: np.ndarray) -> List[List[float]]:
        """Convert control points to polygon points

        Args:
            pnt (np.ndarray): Control points

        Returns:
            List[List[float]]: Polygon points
        """
        if self.cfg.MODEL.TRANSFORMER.USE_POLYGON:
            points = pnt.reshape(-1, 2)
        else:
            # bezier to polygon
            u = np.linspace(0, 1, 20)
            pnt = pnt.reshape(2, 4, 2).transpose(0, 2, 1).reshape(4, 4)
            points = np.outer((1 - u) ** 3, pnt[:, 0]) \
                + np.outer(3 * u * ((1 - u) ** 2), pnt[:, 1]) \
                + np.outer(3 * (u ** 2) * (1 - u), pnt[:, 2]) \
                + np.outer(u ** 3, pnt[:, 3])
            points = np.concatenate((points[:, :2], points[:, 2:]), axis=0)

        return points.tolist()

    def process_image(self, input_path: str, output_path: Optional[str] = None) -> List[Dict[str, Any]]:
        """Process images for scene text detection

        Args:
            input_path (str): Path to the input image or directory of images
            output_path (Optional[str], optional): Path to save ouptut images. Defaults to None.

        Returns:
            List[Dict[str, Any]]: List of results containing detected text instances
        """
        if os.path.isdir(input_path):
            input_path_list = [os.path.join(input_path, fname) for fname in os.listdir(input_path)]
        else:
            input_path_list = glob.glob(os.path.expanduser(input_path))
        
        assert input_path_list, "No input images found"
        if output_path:
            os.makedirs(output_path, exist_ok=True)

        results = []
        for path in tqdm(input_path_list):
            img = cv2.cvtColor(cv2.imread(path), cv2.COLOR_BGR2RGB)
            start_time = time.time()
            predictions, visualized_output = self.demo.run_on_image(
                image=img  
            )
        
            self.logger.info(
                "{}: detected {} instances in {:.2f}s".format(
                    path, len(predictions["instances"]), time.time() - start_time
                )
            )

            cv_image = cv2.cvtColor(np.array(visualized_output.get_image()), cv2.COLOR_RGB2BGR)
            instances = predictions['instances'].to('cpu')
            if self.cfg.MODEL.TRANSFORMER.USE_POLYGON:
                polygons = instances.polygons
            else:
                polygons = instances.beziers
            
            polygon_points = [self.ctrl_pnt_to_poly(
                poly.numpy()
            ) for poly in polygons]

            scores = instances.scores.tolist()

            results.append(
                {
                    'PIL_img': cv_image,
                    'polygons': polygon_points,
                    'scores': scores    
                }
            )

            if output_path:
                # Use the basename of the input path for the output filename
                input_basename = os.path.splitext(os.path.basename(path))[0]
                out_filename = os.path.join(output_path, f"{input_basename}.webp")
                cv2.imwrite(out_filename, cv_image, [cv2.IMWRITE_WEBP_QUALITY, 80])
        
        return results
     

    def visluaize_results(self, results: List[Dict[str, Any]]):
        """Visualize the text detection results.

        Args:
            results (List[Dict[str, Any]]): List of results from process_image method
        """

        for i, result in enumerate(results):
            img = result['PIL_img']
            polygons = result['polygons']
            scores = result['scores']

            plt.figure(figsize=(12, 8))
            plt.imshow(img)

            for polygon, score in zip(polygons, scores):
                polygon = np.array(polygon)
                plt.plot(polygon[:, 0], polygon[:, 1], 'r-')
                plt.text(polygon[0, 0], polygon[0, 1], f'{score:.2f}', color='white', 
                         bbox=dict(facecolor='red', alpha=0.5))

            plt.title(f"Detected Text Instances in Image {i+1}")
            plt.axis('off')
            plt.show()
        
    
    def detect_text(self, input_path: str, output_path: Optional[str] = None, visualize: bool = False) -> List[Dict[str, Any]]:
        """Detect text in images and optionally visualize the results

        Args:
            input_path (str): Pat to input image or directory of images
            output_path (Optional[str], optional): Path to save output images. Defaults to None.
            visualize (bool, optional): Whether to visualuze the results. Defaults to False.

        Returns:
            List[Dict[str, Any]]: List of results containing the detected text instances
        """

        results = self.process_image(
            input_path= input_path,
            output_path= output_path
        )
        if visualize:
            self.visluaize_results(results=results)
        return results
                
