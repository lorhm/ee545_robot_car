#!/usr/bin/env python

from __future__ import (
    division,
)  # because python 2.7 division is a bug-causing nightmare
import rospy
import numpy as np
import math
import sys

import utils
from tf import transformations

from sensor_msgs.msg import LaserScan
from ackermann_msgs.msg import AckermannDriveStamped
from geometry_msgs.msg import PoseStamped, PoseArray, Pose, Quaternion

SCAN_TOPIC = "/scan"  # The topic to subscribe to for laser scans
CMD_TOPIC = (
    "/vesc/high_level/ackermann_cmd_mux/input/nav_0"
)  # The topic to publish controls to
POSE_TOPIC = (
    "/sim_car_pose/pose"
)  # The topic to subscribe to for current pose of the car
# NOTE THAT THIS IS ONLY NECESSARY FOR VIZUALIZATION
VIZ_TOPIC = "/laser_wanderer/rollouts"  # The topic to publish to for vizualizing
# the computed rollouts. Publish a PoseArray.

MAX_PENALTY = 10000  # The penalty to apply when a configuration in a rollout
# goes beyond the corresponding laser scan
CAR_WIDTH = 0.3  # Total guess TBH. Need to check this - JH
DETECTION_THRESH = 0.5  # Would be good to add this as a param to the launchfile

"""
Wanders around using minimum (steering angle) control effort while avoiding crashing
based off of laser scans.
"""


class LaserWanderer:

    """
  Initializes the LaserWanderer
    rollouts: An NxTx3 numpy array that contains N rolled out trajectories, each
              containing T poses. For each trajectory, the t-th element represents
              the [x,y,theta] pose of the car at time t+1
    deltas: An N dimensional array containing the possible steering angles. The n-th
            element of this array is the steering angle that would result in the
            n-th trajectory in rollouts
    speed: The speed at which the car should travel
    compute_time: The amount of time (in seconds) we can spend computing the cost
    laser_offset: How much to shorten the laser measurements
  """

    def __init__(
        self,
        rollouts,
        deltas,
        speed,
        compute_time,
        laser_offset,
        car_length,
        car_width,
        detection_thresh,
    ):
        # Store the params for later
        self.rollouts = rollouts
        self.deltas = deltas
        self.speed = speed
        self.compute_time = compute_time
        self.laser_offset = laser_offset
        self.detection_thresh = detection_thresh
        self.car_length = car_length
        self.car_width = car_width

        # YOUR CODE HERE
        self.cmd_pub = rospy.Publisher(CMD_TOPIC, AckermannDriveStamped, queue_size=1)
        self.laser_sub = rospy.Subscriber(SCAN_TOPIC, LaserScan, self.wander_cb)
        self.viz_pub = rospy.Publisher(
            VIZ_TOPIC, PoseArray, queue_size=1
        )  # Create a publisher for vizualizing trajectories. Will publish PoseArrays
        self.viz_sub = rospy.Subscriber(
            POSE_TOPIC, PoseStamped, self.vizsub_cb
        )  # Create a subscriber to the current position of the car
        # NOTE THAT THIS VIZUALIZATION WILL ONLY WORK IN SIMULATION. Why?

    """
  Vizualize the rollouts. Transforms the rollouts to be in the frame of the world.
  Only display the last pose of each rollout to prevent lagginess
    msg: A PoseStamped representing the current pose of the car
  """

    def vizsub_cb(self, msg):
        # Create the PoseArray to publish. Will contain N poses, where the n-th pose
        # represents the last pose in the n-th trajectory
        pa = PoseArray()
        pa.header.frame_id = "/map"
        pa.header.stamp = rospy.Time.now()
        self.current_pose = [
            msg.pose.position.x,
            msg.pose.position.y,
            utils.quaternion_to_angle(msg.pose.orientation),
        ]
        # Transform the last pose of each trajectory to be w.r.t the world and insert into
        # the pose array
        # YOUR CODE HERE
        for n in range(self.rollouts.shape[0]):
            M = transformations.compose_matrix(
                translate=(self.current_pose[0], self.current_pose[1], 0),
                angles=(0, 0, self.current_pose[2]),
            )
            # M_inv = np.linalg.inv(M)
            [x, y, z, w] = np.dot(
                M, np.array([self.rollouts[n][-1][0], self.rollouts[n][-1][1], 0, 1]).T
            )

            pose = Pose()
            xyzw_array = lambda o: np.array([o.x, o.y, o.z, o.w])
            quat1 = [
                msg.pose.orientation.x,
                msg.pose.orientation.y,
                msg.pose.orientation.z,
                msg.pose.orientation.w,
            ]
            quat2_raw = utils.angle_to_quaternion(self.rollouts[n][-1][2])
            quat2 = xyzw_array(quat2_raw)
            # quat2 = [self.rollouts[n][-1][2][0], self.rollouts[n][-1][2][1], self.rollouts[n][-1][2][2], self.rollouts[n][-1][2][3]]
            # rospy.loginfo("Quaternion:")
            # rospy.loginfo(utils.angle_to_quaternion(self.rollouts[n][-1][2:]))
            # rospy.loginfo("\n")
            orientation_raw = transformations.quaternion_multiply(quat1, quat2)
            # rospy.loginfo(orientation_raw)
            pose.orientation = Quaternion(*orientation_raw)
            pose.position.x = x
            pose.position.y = y
            pose.position.z = 0
            pa.poses.append(pose)

        self.viz_pub.publish(pa)

    """
  current pose: [x,y,theta]
  rollout pose: [x,y,theta]
  return angle between current pose's x-axis and the rollout pose
  """

    def _compute_pose_angle(self, current_pose, rollout_pose):
        delta_x = (
            rollout_pose[0] - current_pose[0]
        )  # What is the point of this step? current_pose[0]==0
        delta_y = (
            rollout_pose[1] - current_pose[1]
        )  # What is the point of this step? current_pose[0]==0
        return np.arctan(delta_y / delta_x)

    """
  Compute the cost of one step in the trajectory. It should penalize the magnitude
  of the steering angle. It should also heavily penalize crashing into an object
  (as determined by the laser scans)
    delta: The steering angle that corresponds to this trajectory
    rollout_pose: The pose in the trajectory
    laser_msg: The most recent laser scan
  """

    def compute_cost(self, delta, rollout_pose, laser_msg):

        # Initialize the cost to be the magnitude of delta
        # Consider the line that goes from the robot to the rollout pose
        # Compute the angle of this line with respect to the robot's x axis
        # Find the laser ray that corresponds to this angle
        # Add MAX_PENALTY to the cost if the distance from the robot to the rollout_pose
        # is greater than the laser ray measurement - np.abs(self.laser_offset)
        # Return the resulting cost
        # Things to think about:
        #   What if the angle of the pose is less (or greater) than the angle of the
        #   minimum (or maximum) laser scan angle
        #   What if the corresponding laser measurement is NAN?
        # NOTE THAT NO COORDINATE TRANSFORMS ARE NECESSARY INSIDE OF THIS FUNCTION
        # rospy.loginfo('%s' % rollout_pose)
        # YOUR CODE HERE
        cost = np.absolute(delta)
        # rospy.loginfo("Delta Cost:")
        # rospy.loginfo(cost)
        angle_index = []
        too_close_count = 0
        current_pose = [0, 0, 0]
        # rollout_pose_angle = self._compute_pose_angle(current_pose, rollout_pose)
        rollout_pose_angle = self.rollout_to_laser_angle(rollout_pose)
        rollout_pose_distance = np.linalg.norm(
            np.array(current_pose[:-1]) - np.array(rollout_pose[:-1])
        )

        # if the laser can't see this position, assign max penalty, (e.g. assume that there's something there)
        if (
            min(rollout_pose_angle) < laser_msg.angle_min
            or max(rollout_pose_angle) > laser_msg.angle_max
        ):
            cost += MAX_PENALTY
            return cost
        for roll_angle in [min(rollout_pose_angle), max(rollout_pose_angle)]:
            angle_index.append(
                (int)((roll_angle - laser_msg.angle_min) / laser_msg.angle_increment)
            )
        angle_index = np.arange(min(angle_index), max(angle_index) + 1)
        rospy.loginfo(angle_index)
        for idx in angle_index:
            laser_ray_dist = laser_msg.ranges[idx]
            # rospy.loginfo("dist: %s (wanted_pose: %s)| laser_ray_dist: %s" % (rollout_pose_distance,rollout_pose,laser_ray_dist ))
            if np.isfinite(laser_ray_dist) and rollout_pose_distance > (
                laser_ray_dist - np.abs(self.laser_offset)
            ):
                too_close_count += 1
        hazard_prop = too_close_count / np.size(angle_index)
        if np.size(angle_index) == 1 and too_close_count > 0:
            cost += MAX_PENALTY
        elif hazard_prop >= DETECTION_THRESH:
            cost += MAX_PENALTY
        else:
            cost += hazard_prop * 30
        # rospy.loginfo("Hazard_prop")
        # rospy.loginfo(hazard_prop)
        # rospy.loginfo("Cost:")
        # rospy.loginfo(cost)
        rospy.loginfo("\n")
        return cost

    """
  Controls the steering angle in response to the received laser scan. Uses approximately
  self.compute_time amount of time to compute the control
    msg: A LaserScan
  """

    def wander_cb(self, msg):
        start = rospy.Time.now().to_sec()  # Get the time at which this function started

        # A N dimensional matrix that should be populated with the costs of each
        # trajectory up to time t <= T
        drive_msg = AckermannDriveStamped()
        delta_costs = np.zeros(self.deltas.shape[0], dtype=np.float)
        traj_depth = 0

        # Evaluate the cost of each trajectory. Each iteration of the loop should calculate
        # the cost of each trajectory at time t = traj_depth and add those costs to delta_costs
        # as appropriate

        # Pseudo code
        # while(you haven't run out of time AND traj_depth < T):
        #   for each trajectory n:
        #       delta_costs[n] += cost of the t=traj_depth step of trajectory n
        #   traj_depth += 1
        # YOUR CODE HERE
        T = self.rollouts.shape[1]
        while rospy.Time.now().to_sec() - start < self.compute_time and traj_depth < T:
            for n in range(self.rollouts.shape[0]):
                # rospy.loginfo(delta_costs[n])
                delta_costs[n] += self.compute_cost(
                    self.deltas[n], self.rollouts[n][traj_depth], msg
                )
            traj_depth += 1
        # rospy.loginfo("Trajectory Depth:")
        # rospy.loginfo(traj_depth)
        # rospy.loginfo("\n")
        # Find the delta that has the smallest cost and execute it by publishing
        # YOUR CODE HERE
        # rospy.loginfo("%s" % delta_costs)
        min_delta_index = np.argmin(delta_costs)
        min_delta = self.deltas[min_delta_index]
        drive_msg.header.stamp = rospy.Time.now()
        drive_msg.header.frame_id = "/map"
        drive_msg.drive.steering_angle = min_delta
        drive_msg.drive.speed = self.speed
        self.cmd_pub.publish(drive_msg)

        """
    Take a single pose from a rollout (defined relative the the racecar frame)
        Current pose is assumed to be 0, 0, 0 in its own reference frame
        roll_pose: [x, y, theta] in the base racecar reference frame
    Returns the minimum and maximum angle to sweep with the laserscan
    Note: we could just use our code straight out of lab0 for this, but the direct call to tf is cleaner,
    and hopefully, faster.
  """

    # TODO: Loop this over all of the rollouts at program start and cache it to a file.
    # Add try-except here to try to read that file and generate it if it doesn't exist.
    def rollout_to_laser_angle(self, roll_pose):
        M = transformations.compose_matrix(
            translate=(roll_pose[0], roll_pose[1], 0), angles=(0, 0, roll_pose[2])
        )
        front_left = np.array([self.car_length / 2, self.car_width / 2, 0, 1])
        front_right = np.array([self.car_length / 2, -self.car_width / 2, 0, 1])
        [x, y, z, junk] = np.dot(M, front_left)
        left_angle = np.arctan(y / x)
        [x, y, z, junk] = np.dot(M, front_right)
        right_angle = np.arctan(y / x)
        return (left_angle, right_angle)


###CLASS DEFINITION ENDS

"""
Apply the kinematic model to the passed pose and control
  pose: The current state of the robot [x, y, theta]
  control: The controls to be applied [v, delta, dt]
  car_length: The length of the car
Returns the resulting pose of the robot
"""


def kinematic_model_step(pose, control, car_length):
    # Apply the kinematic model
    # Make sure your resulting theta is between 0 and 2*pi
    # Consider the case where delta == 0.0

    # Calculating Beta

    # if(np.isfinite(tan(control[1])) ) == False):
    #   control[1]+= 0.1

    B = math.atan(0.5 * math.tan(control[1]))
    theta_next = pose[2] + (control[0] / car_length) * (math.sin(2 * B)) * control[2]

    if theta_next < 0:
        theta_next = 2 * math.pi + theta_next

    elif theta_next > 2 * math.pi:
        theta_next = theta_next - math.pi

    # If delta = 0.0, the car is aligned in the required direction, so angle should remain same.
    # delta=0 > tanB = 0 > sinB =0 > theta next = theta
    # x(dot) = x_next - x_prev => x_next = x_prev + speed*cos(theta_next), similarly for y
    if B == 0:
        x_next = pose[0] + control[0] * math.cos(theta_next) * control[2]
        y_next = pose[1] + control[0] * math.sin(theta_next) * control[2]

    else:
        x_next = pose[0] + car_length / math.sin(2 * B) * (
            math.sin(theta_next) - math.sin(pose[2])
        )
        y_next = pose[1] - car_length / math.sin(2 * B) * (
            math.cos(theta_next) - math.cos(pose[2])
        )

    resulting_pose = [x_next, y_next, theta_next]
    # rospy.loginfo("%s", resulting_pose)
    return resulting_pose

    # YOUR CODE HERE
    # pass


"""
Repeatedly apply the kinematic model to produce a trajectory for the car
  init_pose: The initial pose of the robot [x,y,theta]
  controls: A Tx3 numpy matrix where each row is of the form [v,delta,dt]
  car_length: The length of the car
Returns a Tx3 matrix where the t-th row corresponds to the robot's pose at time t+1
"""


def generate_rollout(init_pose, controls, car_length):
    # YOUR CODE HERE
    # pass
    N = controls.shape[0]
    rollout_list = []
    pose = init_pose
    for i in xrange(N):
        pose = kinematic_model_step(pose, controls[i], car_length)
        rollout_list.append(pose)

    rollout = np.asarray(rollout_list)
    # rospy.loginfo('%s' % rollout)
    return rollout


"""
Helper function to generate a number of kinematic car rollouts
    speed: The speed at which the car should travel
    min_delta: The minimum allowed steering angle (radians)
    max_delta: The maximum allowed steering angle (radians)
    delta_incr: The difference (in radians) between subsequent possible steering angles
    dt: The amount of time to apply a control for
    T: The number of time steps to rollout for
    car_length: The length of the car
Returns a NxTx3 numpy array that contains N rolled out trajectories, each
containing T poses. For each trajectory, the t-th element represents the [x,y,theta]
pose of the car at time t+1
"""


def generate_mpc_rollouts(speed, min_delta, max_delta, delta_incr, dt, T, car_length):

    deltas = np.arange(min_delta, max_delta, delta_incr)
    N = deltas.shape[0]

    init_pose = np.array([0.0, 0.0, 0.0], dtype=np.float)

    rollouts = np.zeros((N, T, 3), dtype=np.float)
    for i in xrange(N):
        controls = np.zeros((T, 3), dtype=np.float)
        controls[:, 0] = speed
        controls[:, 1] = deltas[i]
        controls[:, 2] = dt
        rollouts[i, :, :] = generate_rollout(init_pose, controls, car_length)

    return rollouts, deltas


def main():

    rospy.init_node("laser_wanderer", anonymous=True)

    # Load these parameters from launch file
    # We provide suggested starting values of params, but you should
    # tune them to get the best performance for your system
    # Look at constructor of LaserWanderer class for description of each var
    # 'Default' values are ones that probably don't need to be changed (but you could for fun)
    # 'Starting' values are ones you should consider tuning for your system
    # YOUR CODE HERE
    speed = rospy.get_param("~speed")  # Default val: 1.0
    min_delta = rospy.get_param("~min_delta")  # Default val: -0.34
    max_delta = rospy.get_param("~max_delta")  # Default val: 0.341
    delta_incr = rospy.get_param(
        "~delta_incr"
    )  # Starting val: 0.34/3 (consider changing the denominator)
    delta_incr = delta_incr / 3.0
    dt = rospy.get_param("~dt")  # Default val: 0.01
    T = rospy.get_param("~T")  # Starting val: 300
    compute_time = rospy.get_param("~compute_time")  # Default val: 0.09
    laser_offset = rospy.get_param("~laser_offset")  # Starting val: 1.0

    # DO NOT ADD THIS TO YOUR LAUNCH FILE, car_length is already provided by teleop.launch
    car_length = rospy.get_param("car_kinematics/car_length", 0.33)

    # Generate the rollouts
    rollouts, deltas = generate_mpc_rollouts(
        speed, min_delta, max_delta, delta_incr, dt, T, car_length
    )

    # Create the LaserWanderer
    lw = LaserWanderer(
        rollouts,
        deltas,
        speed,
        compute_time,
        laser_offset,
        car_length,
        CAR_WIDTH,
        DETECTION_THRESH,
    )

    # Keep the node alive
    rospy.spin()


if __name__ == "__main__":
    main()

