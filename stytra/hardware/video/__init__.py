"""
Module to interact with video surces as cameras or video files. It also
implement video saving
"""

import numpy as np

from multiprocessing import Queue, Event
from queue import Empty

from lightparam import Param
from lightparam.param_qt import ParametrizedQt

from stytra.utilities import FrameProcess
from arrayqueues.shared_arrays import IndexedArrayQueue
import deepdish as dd

from stytra.hardware.video.cameras import (
    XimeaCamera,
    AvtCamera,
    SpinnakerCamera,
    MikrotronCLCamera,
)

from stytra.hardware.video.write import VideoWriter

from stytra.hardware.video.ring_buffer import RingBuffer

import time


class VideoSource(FrameProcess):
    """Abstract class for a process that generates frames, being it a camera
    or a file source. A maximum size of the memory used by the process can be
    set.
    
    **Input Queues:**

    self.control_queue :
        queue with control parameters for the source, e.g. from a
        :class:`CameraControlParameters <.interfaces.CameraControlParameters>`
        object.


    **Output Queues**

    self.frame_queue :
        TimestampedArrayQueue from the arrayqueues module
        where the frames read from the camera are sent.


    **Events**

    self.kill_signal :
        When set kill the process.


    Parameters
    ----------
    rotation : int
        n of times image should be rotated of 90 degrees
    max_mbytes_queue : int
        maximum size of camera queue (Mbytes)

    Returns
    -------

    """

    def __init__(self, rotation=False, max_mbytes_queue=100, n_consumers=1):
        """ """
        super().__init__()
        self.rotation = rotation
        self.control_queue = Queue()
        self.frame_queue = IndexedArrayQueue(max_mbytes=max_mbytes_queue)
        self.kill_event = Event()
        self.n_consumers = 1


class CameraSource(VideoSource):
    """Process for controlling a camera.

    Cameras currently implemented:
    
    ======== ===========================================
    Ximea    Add some info
    Avt      Add some info
    ======== ===========================================

    Parameters
    ----------
    camera_type : str
        specifies type of the camera (currently supported: 'ximea', 'avt')
    downsampling : int
        specifies downsampling factor for the camera.

    Returns
    -------

    """

    camera_class_dict = dict(
        ximea=XimeaCamera,
        avt=AvtCamera,
        spinnaker=SpinnakerCamera,
        mikrotron=MikrotronCLCamera,
    )
    """ dictionary listing classes used to instantiate camera object."""

    def __init__(
        self, camera_type, *args, downsampling=1, roi=(-1, -1, -1, -1), **kwargs
    ):
        """ """
        super().__init__(*args, **kwargs)

        self.cam = None

        self.camera_type = camera_type
        self.downsampling = downsampling
        self.roi = roi

        self.state = CameraControlParameters()
        self.ring_buffer = None

    def run(self):
        """
        After initializing the camera, the process constantly does the
        following:

            - read control parameters from the control_queue and set them;
            - read frames from the camera and put them in the frame_queue.


        """
        try:
            CameraClass = self.camera_class_dict[self.camera_type]
            self.cam = CameraClass(downsampling=self.downsampling, roi=self.roi)
        except KeyError:
            raise Exception("{} is not a valid camera type!".format(self.camera_type))
        self.message_queue.put("I:" + str(self.cam.open_camera()))
        prt = None
        while True:
            # Kill if signal is set:
            self.kill_event.wait(0.0001)
            if self.kill_event.is_set():
                break

            # Try to get new parameters from the control queue:
            message = ""
            if self.control_queue is not None:
                while True:
                    try:
                        param_dict = self.control_queue.get(timeout=0.0001)
                        self.state.values = param_dict
                        for param, value in param_dict.items():
                            message = self.cam.set(param, value)
                    except Empty:
                        break

            # Grab the new frame, and put it in the queue if valid:
            arr = self.cam.read()
            if self.rotation:
                arr = np.rot90(arr, self.rotation)
            if self.ring_buffer is None or self.state.ring_buffer_length != self.state.ring_buffer.length:
                self.ring_buffer = RingBuffer(self.state.ring_buffer_length)

            self.update_framerate()

            if self.state.paused:
                self.frame_queue.put(self.ring_buffer.get_most_recent())
            elif self.state.replay and self.state.replay_fps > 0:
                try:
                    self.frame_queue.put(self.ring_buffer.get())
                except ValueError:
                    pass
                delta_t = 1 / self.state.replay_fps
                if prt is not None:
                    extrat = delta_t - (time.process_time() - prt)
                    if extrat > 0:
                        time.sleep(extrat)
                prt = time.process_time()
            else:
                self.ring_buffer.put(arr)
                prt = None
                if arr is not None and not self.state.paused:
                    # If the queue is full, arrayqueues should print a warning!
                    if self.frame_queue.queue.qsize() < self.n_consumers + 2:
                        self.frame_queue.put(arr)
                    else:
                        self.message_queue.put("W:Dropped frame")

        self.cam.release()


class VideoFileSource(VideoSource):
    """A class to stream videos from a file to test parts of
    stytra without a camera available, or do offline analysis

    Parameters
    ----------
        source_file
            path of the video file
        loop : bool
            continue video from the beginning if the end is reached

    Returns
    -------

    """

    def __init__(self, source_file=None, loop=True, framerate=None, **kwargs):
        super().__init__(**kwargs)
        self.source_file = source_file
        self.loop = loop
        self.framerate = framerate
        self.control_params = VideoControlParameters
        self.offset = 0
        self.paused = False
        self.old_frame = None
        self.offset = 0


    def inner_loop(self):
        pass

    def run(self):

        if self.source_file.endswith("h5"):
            framedata = dd.io.load(self.source_file)
            frames = framedata["video"]
            if self.framerate is None:
                delta_t = 1 / framedata.get("framerate", 30.0)
            else:
                delta_t = 1 / self.framerate
            i_frame = self.offset
            prt = None
            while not self.kill_event.is_set():

                # Try to get new parameters from the control queue:
                message = ""
                if self.control_queue is not None:
                    while True:
                        try:
                            param_dict = self.control_queue.get(timeout=0.0001)
                            for name, value in param_dict.items():
                                if name == "framerate":
                                    delta_t = 1 / value
                                elif name == "offset":
                                    if value != self.offset:
                                        self.offset = value
                                elif name == "paused":
                                    self.paused = value
                        except Empty:
                            break

                # we adjust the framerate
                if prt is not None:
                    extrat = delta_t - (time.process_time() - prt)
                    if extrat > 0:
                        time.sleep(extrat)

                self.frame_queue.put(frames[i_frame, :, :])
                if not self.paused:
                    i_frame += 1
                if i_frame == frames.shape[0]:
                    if self.loop:
                        i_frame = self.offset
                    else:
                        break
                self.update_framerate()
                prt = time.process_time()
        else:
            import cv2

            cap = cv2.VideoCapture(self.source_file)
            ret = True

            if self.framerate is None:
                try:
                    delta_t = 1 / cap.get(cv2.CAP_PROP_FPS)
                except ZeroDivisionError:
                    delta_t = 1 / 30
            else:
                delta_t = 1 / self.framerate

            prt = None
            while ret and not self.kill_event.is_set():
                if self.paused:
                    ret = True
                    frame = self.old_frame
                else:
                    ret, frame = cap.read()

                # adjust the frame rate by adding extra time if the processing
                # is quicker than the specified framerate

                if self.control_queue is not None:
                    try:
                        param_dict = self.control_queue.get(timeout=0.0001)
                        for name, value in param_dict.items():
                            if name == "framerate":
                                delta_t = 1 / value
                            elif name == "offset":
                                if value != self.offset:
                                    cap.set(cv2.CAP_PROP_POS_FRAMES, value)
                                    self.offset = value
                            elif name == "paused":
                                self.paused = value
                    except Empty:
                        pass

                if prt is not None:
                    extrat = delta_t - (time.process_time() - prt)
                    if extrat > 0:
                        time.sleep(extrat)

                if ret:
                    self.frame_queue.put(frame[:, :, 0])
                else:
                    if self.loop:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        ret = True
                    else:
                        break

                prt = time.process_time()
                self.old_frame = frame
                self.update_framerate()
            return


class VideoControlParameters(ParametrizedQt):
    def __init__(self, **kwargs):
        super().__init__(name="video_params", **kwargs)
        self.framerate = Param(150., limits=(10, 700), unit="Hz", desc="Framerate (Hz)")
        self.offset = Param(50)
        self.paused = Param(False)


class CameraControlParameters(ParametrizedQt):
    """HasPyQtGraphParams class for controlling the camera params.
    Ideally, methods to automatically set dynamic boundaries on frame rate and
    exposure time can be implemented. Currently not implemented.

    Parameters
    ----------

    Returns
    -------

    """

    def __init__(self, **kwargs):
        super().__init__(name="camera_params", **kwargs)
        self.exposure = Param(1., limits=(0.1, 50), unit="ms", desc="Exposure (ms)")
        self.framerate = Param(
            150., limits=(10, 700), unit=" Hz", desc="Framerate (Hz)"
        )
        self.gain = Param(1., limits=(0.1, 12), desc="Camera amplification gain")
        self.ring_buffer_length = Param(
            600, (1, 2000), desc="Rolling buffer that saves the last items",
            gui=False
        )
        self.replay = Param(
           True,
            desc="Replaying",
            gui=False
        )
        self.replay_fps = Param(
            15,
            (0, 500),
            desc="If bigger than 0, the rolling buffer will be replayed at the given framerate",
        )
        self.replay_limits = Param((0,0), gui=False)