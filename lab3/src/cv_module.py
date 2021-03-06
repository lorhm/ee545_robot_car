#!/usr/bin/env python

import cv2
import time
import sys
import rospy
import rosbag
import numpy as np
import scipy.signal
import utils as Utils
from std_msgs.msg import String
from sensor_msgs.msg import Image
import math
from ackermann_msgs.msg import AckermannDriveStamped
from cv_bridge import CvBridge, CvBridgeError
from collections import deque
from std_msgs.msg import Float32

IMAGE_TOPIC = '/camera/color/image_raw'
CMD_TOPIC = '/vesc/high_level/ackermann_cmd_mux/input/nav_0' # The topic to publish controls to
IMGPUB_TOPIC = '/cv_module/image_op'
PID_ERROR_TOPIC = '/cv_module/piderror'
HANDOFF_TOPIC = "controller/handoff"

class RBFilter:
    def __init__(self, min_angle, max_angle, angle_incr,speed, kp, ki, kd, error_buff_length):
        # Storing Params if needed
        self.min_angle = min_angle
        self.max_angle = max_angle
        self.angle_incr = angle_incr
        self.angles = np.arange(min_angle, max_angle, angle_incr)
        self.speed = speed
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.bridge = CvBridge()
        self.error_buff = deque(maxlen=error_buff_length)
        self.center = dict()
        self.red_x = None
        self.red_y = None
        self.blue_x = None
        self.blue_y = None
        self.rgb_img = None
        #Publisher, Subscribers
        self.handoff_sub = rospy.Subscriber(HANDOFF_TOPIC, bool, self.handoff_cb)
        self.img_sub = rospy.Subscriber(IMAGE_TOPIC, Image, self.image_cb)
        self.img_pub = rospy.Publisher(IMGPUB_TOPIC, Image, queue_size= 10)
        self.cmd_pub = rospy.Publisher(CMD_TOPIC, AckermannDriveStamped, queue_size= 5)
        self.pid_err_pub = rospy.Publisher(PID_ERROR_TOPIC, Float32, queue_size = 5)
        self.hsv_img = None
        self.mask_red = None
        self.mask_blue = None
        #For Testing set self.handoff = True
        self.handoff = False

    def handoff_cb(self, msg):
        self.handoff = msg

    """
    Computes the error based on the current pose of the car
    cur_pose: The current pose of the car, represented as a numpy array [x,y,theta]
    Returns: (False, 0.0) if the end of the plan has been reached. Otherwise, returns
    (True, E) - where E is the computed error
    """
    def compute_error(self, centroid_x_pos, img_width):
        x = (((img_width / 2) - centroid_x_pos) / float(img_width)) * self.max_angle
        rospy.loginfo("error")
        rospy.loginfo(x)
        # y = (x+320)/640*0.68 + (-0.34)

        return x

    def is_object_present(self, mask, threshold, color):
        """[summary]

        Arguments:
            mask {[type]} -- [description]
            threshold {[type]} -- [description]
            color {str} -- Either 'red' or 'blue'
        """

        # TODO: Fix self.center for case when red and blue are detected at once
        _, contours, _ = cv2.findContours(mask.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)

        if len(contours ) == 0:
            return False, 0, 0

        # Find the largest contour
        largest_contour = max(contours, key=lambda x:cv2.contourArea(x))

        if  cv2.contourArea(largest_contour)> threshold:
            print "Area of largest Contour", cv2.contourArea(largest_contour)
            M = cv2.moments(largest_contour)
            if M["m00"] != 0:
                cX = int(M["m10"] / M["m00"])
                cY = int(M["m01"] / M["m00"])
                self.center[color] = [cX, cY]
                return True, cX, cY
            else:
                cX, cY = None, None
                self.center[color] = [cX, cY]
                return False, cX, cY


    def hsv_thresh(self, hsv_samp, tol=[5, 25, 25]):
        # Define HSV Parameter bounds for CV2
        CV_MAX_HSV = [180, 255, 255]
        CV_MIN_HSV = [0, 0, 0]

        # HUE_SHIFT = tol[0] + 1
        HUE_SHIFT = 0
        # Convert PS HSV form [0-360, 0-100, 0-100] to CV2 form [0-180, 0-255, 0-255]
        hsv_cv = np.zeros_like(hsv_samp)
        hsv_cv[0] = int(round(hsv_samp[0]/2)) - HUE_SHIFT
        hsv_cv[1] = int(round((hsv_samp[1]/100.0)*255))
        hsv_cv[2] = int(round((hsv_samp[2]/100.0)*255))

        lower_hsv = np.array(np.maximum(CV_MIN_HSV, hsv_cv - tol), dtype=int)
        # lower_hsv = np.array([0, int(round(0.2*255)), int(round(0.95*255))], dtype=int)
        upper_hsv = np.array(np.minimum(CV_MAX_HSV, hsv_cv + tol), dtype=int)
        # upper_hsv = np.array([5, int(round(0.8*255)), 255], dtype=int)

        # This is a hacky way of getting around the max red hue value wrapping around 0
        hue_shift_img = self.hsv_img.copy()
        # hue_shift_img[:,:,0] -= HUE_SHIFT
        # hue_shift_img[:, :, 0] = np.array(np.where(hue_shift_img[:, :, 0] < 0, 180 + hue_shift_img[:, :, 0], hue_shift_img[:, :, 0]), dtype=int)
        hsv_mask = cv2.inRange(hue_shift_img, lower_hsv, upper_hsv)

        return hsv_mask

    def area_check(self):
        blue_samp = np.mean([[208, 90, 90], [208, 70, 88], [207,100,96], [204,73,100]], axis=0)  # NOTE: these are in Photoshop HSV and must be converted to CV2's weird ranges
        red_samp = np.mean([[355, 40, 100], [355, 35, 100], [338, 25, 100], [356,58,100]], axis=0) # NOTE: these are in Photoshop HSV and must be converted to CV2's weird ranges
        # red_rollover =[2, 28,100]


        BLUE_TOL = [40, 90, 90]   # Blue hue tolerance
        RED_TOL = [40, 90, 60]  # Red hue tolerance
        self.rgb_img = cv2.GaussianBlur(self.rgb_img,(11,11),0)
        self.hsv_img = cv2.cvtColor(self.rgb_img, cv2.COLOR_BGR2HSV)
        self.mask_red = self.hsv_thresh(red_samp, RED_TOL)
        self.mask_blue = self.hsv_thresh(blue_samp, BLUE_TOL)

        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(5,5))

        self.mask_red = cv2.morphologyEx(self.mask_red, cv2.MORPH_CLOSE, kernel)
        self.mask_red = cv2.dilate(self.mask_red,kernel,iterations = 1)
        self.mask_red  = cv2.erode(self.mask_red, None, iterations = 2)

        self.mask_blue = cv2.morphologyEx(self.mask_blue, cv2.MORPH_CLOSE, kernel)
        self.mask_blue = cv2.dilate(self.mask_blue,kernel,iterations = 1)
        self.mask_blue  = cv2.erode(self.mask_blue, None, iterations = 2)

        # cv2.imshow("blue_mask", self.mask_blue)
        # cv2.imshow("red_mask", self.mask_red)

        # Adjust threshold based on output
        square_area_threshold = 300
        # TODO - it might be better if we adjust this threshold dynamically based on either depth info from the RealSense or our best guess as to distance-to-target based on ParticleFilter
        # Dead simple version - do it as a function of the centroid height?

        red_square_present, self.red_x, self.red_y = self.is_object_present(self.mask_red, square_area_threshold, color='red')
        if red_square_present:
            cv2.circle(self.rgb_img, (self.red_x, self.red_y), 7, (255, 255, 255), -1)
            print " Red X, Y", self.red_x, self.red_y

        blue_square_present, self.blue_x, self.blue_y = self.is_object_present(self.mask_blue, square_area_threshold, color='blue')
        if blue_square_present:
            cv2.circle(self.rgb_img, (self.blue_x, self.blue_y), 7, (255, 255, 255), -1)
            print " Blue X, Y", self.blue_x, self.blue_y

        if(red_square_present):
            print "Red present"

        if(blue_square_present):
            print "Blue present"
        #Now order of precedence would be for red over blue

        # If both red and blue detected, deal with the one that is closer (lower y_val) first
        if red_square_present and blue_square_present:
            if self.blue_y > self.red_y:
                # Blue centroid is closer than red centroid (roughly - this doesn't really factor in angled trajectories)
                self.publish_control('blue')
            else:
                self.publish_control('red')
        # elif red is detected, use that controller
        elif red_square_present:
            self.publish_control('red')
        # elif blue is detected, use that controller
        elif blue_square_present:
            self.publish_control('blue')

        # cv2.imshow("HSV Image", hsv)
        # cv2.imshow("BGR8 Image", rgb_img)
    def compute_steering_angle_blue(self, error):
        now = rospy.Time.now().to_sec()  # Get the current time

        # Compute the derivative error using the passed error, the current time,
        # the most recent error stored in self.error_buff, and the most recent time
        # stored in self.error_buff
        deriv_error = 0
        if len(self.error_buff) > 0:

            [last_time, last_error] = self.error_buff[-1]
            deriv_error = (error - last_error) / (now - last_time)

        # Compute the integral error by applying rectangular integration to the elements
        integ_error = 0
        next_time = now
        next_error = error
        for elem in reversed(self.error_buff):
            error_element = (elem[1] + next_error) / 2 * (next_time - elem[0])
            integ_error = integ_error + error_element

            # Store the next time
            next_time = elem[0]

        # Add the current error to the buffer
        self.error_buff.append((now, error))

        # Compute the steering angle as the sum of the pid errors
        steering_angle = self.kp * error + self.ki * integ_error + self.kd * deriv_error
        rospy.loginfo("Steering Angle")
        rospy.loginfo(steering_angle)
        self.pid_err_pub.publish(Float32(steering_angle))
        steering_angle = np.sign(steering_angle) * min(abs(steering_angle), self.max_angle)
        # steering_angle = np.arctan(steering_angle)*0.68/np.pi
        # assert ((steering_angle >= self.min_angle) and (steering_angle <= self.max_angle)), "turn_angle = {}, outside range [{}, {}]".format(steering_angle, self.min_angle, self.max_angle)
        return steering_angle

    def compute_steering_angle_red ( self, x, img_width ):
        #dimensions of the rgb image and hsv image are same.
        if x < self.center['red'][0]: # red to the left
            turn_angle = self.angles[-1] # we want to turn to the right ASAP
        else: # red to the right
            turn_angle = self.angles[0] # we want to turn to the left ASAP
        assert ((turn_angle >= self.min_angle) and (turn_angle <= self.max_angle)), "turn_angle = {}, outside range [{}, {}]".format(turn_angle, self.min_angle, self.max_angle)
        return turn_angle

    def publish_control(self, color):
        if not (self.cmd_pub.get_num_connections() > 0):
            return
        if color is 'red':
            turn_angle = self.compute_steering_angle_red(self.red_x, self.rgb_img.shape[1])
            print "Red present, turn - ", turn_angle

        elif color is 'blue':
            error = self.compute_error(self.blue_x, self.rgb_img.shape[1])
            turn_angle = self.compute_steering_angle_blue(error)
            print "Blue present, turn - ", turn_angle
        else:
            raise ColorNotSpecified('Argument \'color\' was not one of [\'red\', \'blue\']')

        drive_msg = AckermannDriveStamped()
        drive_msg.header = Utils.make_header('map')
        drive_msg.drive.steering_angle = turn_angle
        drive_msg.drive.speed = self.speed
        self.cmd_pub.publish(drive_msg)

    def image_cb(self,data):
        # print('callback')
        try:
            self.rgb_img = cv2.resize(self.bridge.imgmsg_to_cv2(data, "bgr8"), fx=0.5, fy=0.5, interpolation=INTER_LINEAR) 
        except CvBridgeError as e:
            print(e)

        # cv2.imshow("BGR8 Image", self.rgb_img)

        if self.handoff:
            self.area_check()

        cv2.waitKey(3)

        if self.img_pub.get_num_connections() > 0:
            try:
                self.img_pub.publish(self.bridge.cv2_to_imgmsg(self.rgb_img, "bgr8"))
            except CvBridgeError as e:
                print(e)


def main():
    rospy.init_node('cv_module', anonymous=True)
    speed = rospy.get_param('~speed')#default val:1
    speed = speed / 2.0
    print speed
    min_angle = rospy.get_param('~min_angle')# Default val: -0.34
    max_angle = rospy.get_param('~max_angle')# Default val: 0.341
    angle_incr = rospy.get_param('~angle_incr')# Starting val: 0.34/3 (consider changing the denominator)
    angle_incr /= 3
    kp = rospy.get_param("~kp", None)
    ki = rospy.get_param("~ki", None)
    kd = rospy.get_param("~kd", None)
    error_buff_length = rospy.get_param("~error_buff_length", None)
    im_filter = RBFilter(min_angle, max_angle, angle_incr, speed, kp, ki, kd, error_buff_length)

    while not rospy.is_shutdown():
        rospy.sleep(1)

    cv2.destroyAllWindows()



if __name__ == '__main__':
    main()
