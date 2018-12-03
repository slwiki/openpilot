import zmq
import math
import numpy as np
import time
import json
from selfdrive.controls.lib.pid import PIController
from selfdrive.controls.lib.drive_helpers import MPC_COST_LAT
from selfdrive.controls.lib.lateral_mpc import libmpc_py
from common.numpy_fast import interp
from common.realtime import sec_since_boot
from selfdrive.swaglog import cloudlog
from cereal import car

_DT = 0.01    # 100Hz
_DT_MPC = 0.05  # 20Hz


def calc_states_after_delay(states, v_ego, steer_angle, curvature_factor, steer_ratio, delay):
  states[0].x = v_ego * delay
  states[0].psi = v_ego * curvature_factor * math.radians(steer_angle) / steer_ratio * delay
  return states


def get_steer_max(CP, v_ego):
  return interp(v_ego, CP.steerMaxBP, CP.steerMaxV)

def apply_deadzone(angle, deadzone):
  if angle > deadzone:
    angle -= deadzone
  elif angle < -deadzone:
    angle += deadzone
  else:
    angle = 0.
  return angle

class LatControl(object):
  def __init__(self, CP):
    self.pid = PIController((CP.steerKpBP, CP.steerKpV),
                            (CP.steerKiBP, CP.steerKiV),
                            k_f=CP.steerKf, pos_limit=1.0)
    self.last_cloudlog_t = 0.0
    self.setup_mpc(CP.steerRateCost)
    self.context = zmq.Context()
    self.steerpub = self.context.socket(zmq.PUB)
    self.steerpub.bind("tcp://*:8594")
    self.steerdata = ""
    self.smooth_factor = CP.steerActuatorDelay / _DT
    self.feed_forward = 0.0
    self.angle_rate_desired = 0.0
    self.ff_angle_factor = 1.0
    self.ff_rate_factor = self.ff_angle_factor * 3.0
    self.accel_limit = 5.0
    self.curvature_factor = 0.0
    self.slip_factor = 0.0

  def setup_mpc(self, steer_rate_cost):
    self.libmpc = libmpc_py.libmpc
    self.libmpc.init(MPC_COST_LAT.PATH, MPC_COST_LAT.LANE, MPC_COST_LAT.HEADING, steer_rate_cost)

    self.mpc_solution = libmpc_py.ffi.new("log_t *")
    self.cur_state = libmpc_py.ffi.new("state_t *")
    self.mpc_updated = False
    self.mpc_nans = False
    self.cur_state[0].x = 0.0
    self.cur_state[0].y = 0.0
    self.cur_state[0].psi = 0.0
    self.cur_state[0].delta = 0.0

    self.last_mpc_ts = 0.0
    self.angle_steers_des = 0.0
    self.angle_steers_des_mpc = 0.0
    self.angle_steers_des_prev = 0.0
    self.angle_steers_des_time = 0.0

  def reset(self):
    self.pid.reset()

  def update(self, active, v_ego, angle_steers, angle_rate, steer_override, d_poly, angle_offset, CP, VM, PL):
    cur_time = sec_since_boot()
    self.mpc_updated = False
    # TODO: this creates issues in replay when rewinding time: mpc won't run
    if self.last_mpc_ts < PL.last_md_ts:
      self.last_mpc_ts = PL.last_md_ts
      self.angle_steers_des_prev = self.angle_steers_des_mpc

      self.curvature_factor = VM.curvature_factor(v_ego)

      self.l_poly = libmpc_py.ffi.new("double[4]", list(PL.PP.l_poly))
      self.r_poly = libmpc_py.ffi.new("double[4]", list(PL.PP.r_poly))
      self.p_poly = libmpc_py.ffi.new("double[4]", list(PL.PP.p_poly))

      # account for actuation delay
      self.cur_state = calc_states_after_delay(self.cur_state, v_ego, (CP.steerActuatorDelay * angle_rate + angle_steers), self.curvature_factor, CP.steerRatio, CP.steerActuatorDelay)

      v_ego_mpc = max(v_ego, 5.0)  # avoid mpc roughness due to low speed
      self.libmpc.run_mpc(self.cur_state, self.mpc_solution,
                          self.l_poly, self.r_poly, self.p_poly,
                          PL.PP.l_prob, PL.PP.r_prob, PL.PP.p_prob, self.curvature_factor, v_ego_mpc, PL.PP.lane_width)

      # reset to current steer angle if not active or overriding
      if active:
        self.isActive = 1
        delta_desired = self.mpc_solution[0].delta[1]
      else:
        self.isActive = 0
        delta_desired = math.radians(angle_steers - angle_offset) / CP.steerRatio

      self.cur_state[0].delta = delta_desired

      self.angle_steers_des_mpc = float(math.degrees(delta_desired * CP.steerRatio) + angle_offset)
      self.angle_steers_des_time = float(PL.last_md_ts / 1000000000.0)
      self.angle_rate_desired = (self.angle_steers_des_mpc - self.angle_steers_des_prev) / _DT_MPC
      self.mpc_updated = True

      #  Check for infeasable MPC solution
      self.mpc_nans = np.any(np.isnan(list(self.mpc_solution[0].delta)))
      t = sec_since_boot()
      if self.mpc_nans:
        self.libmpc.init(MPC_COST_LAT.PATH, MPC_COST_LAT.LANE, MPC_COST_LAT.HEADING, CP.steerRateCost)
        self.cur_state[0].delta = math.radians(angle_steers) / CP.steerRatio

        if t > self.last_cloudlog_t + 5.0:
          self.last_cloudlog_t = t
          cloudlog.warning("Lateral mpc - nan: True")

    elif self.steerdata != "" and len(self.steerdata) > 40000:
      self.steerpub.send(self.steerdata)
      self.steerdata = ""

    if v_ego < 0.3 or not active:
      output_steer = 0.0
      self.feed_forward = 0.0
      self.pid.reset()
    else:
      self.angle_steers_des = self.angle_steers_des_prev + self.angle_rate_desired * (cur_time - self.angle_steers_des_time)
      restricted_steer_rate = np.clip(self.angle_steers_des - float(angle_steers) , float(angle_rate) - self.accel_limit, float(angle_rate) + self.accel_limit)
      future_angle_steers_des = float(angle_steers) + self.smooth_factor * _DT * restricted_steer_rate
      future_angle_steers = float(angle_steers) + self.smooth_factor * _DT * float(angle_rate)

      steers_max = get_steer_max(CP, v_ego)
      self.pid.pos_limit = steers_max
      self.pid.neg_limit = -steers_max
      if CP.steerControlType == car.CarParams.SteerControlType.torque:
        if abs(self.ff_rate_factor * float(restricted_steer_rate)) > abs(self.ff_angle_factor * float(future_angle_steers_des) - float(angle_offset)) - 0.5 \
            and (abs(float(restricted_steer_rate)) > abs(angle_rate) or (float(restricted_steer_rate) < 0) != (angle_rate < 0)) \
            and (float(restricted_steer_rate) < 0) == (float(future_angle_steers_des) - float(angle_offset) - 0.5 < 0):
          ff_type = "r"
          self.feed_forward = (((self.smooth_factor - 1.) * self.feed_forward) + self.ff_rate_factor * v_ego**2 * float(restricted_steer_rate)) / self.smooth_factor  
        elif abs(future_angle_steers_des - float(angle_offset)) > 0.5:
          ff_type = "a"
          self.feed_forward = (((self.smooth_factor - 1.) * self.feed_forward) + self.ff_angle_factor * v_ego**2 * float(apply_deadzone(float(future_angle_steers_des) - float(angle_offset), 0.5))) / self.smooth_factor  
        else:
          ff_type = "r"
          self.feed_forward = (((self.smooth_factor - 1.) * self.feed_forward) + 0.0) / self.smooth_factor  
      else:
        self.feed_forward = self.angle_steers_des_mpc   # feedforward desired angle
      deadzone = 0.0
      output_steer = self.pid.update(self.angle_steers_des, angle_steers, check_saturation=(v_ego > 10), override=steer_override,
                                     feedforward=self.feed_forward, speed=v_ego, deadzone=deadzone)

      self.pCost = 0.0
      self.lCost = 0.0
      self.rCost = 0.0
      self.hCost = 0.0
      self.srCost = 0.0
      self.last_ff_a = 0.0
      self.last_ff_r = 0.0
      self.center_angle = 0.0
      self.steer_zero_crossing = 0.0
      self.steer_rate_cost = 0.0
      self.avg_angle_rate = 0.0
      self.angle_rate_count = 0.0
      driver_torque = 0.0     
      steer_motor = 0.0

      self.steerdata += ("%d,%s,%d,%d,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%d|" % (self.isActive, \
      ff_type, 1 if ff_type == "a" else 0, 1 if ff_type == "r" else 0, self.cur_state[0].x, self.cur_state[0].y, self.cur_state[0].psi, self.cur_state[0].delta, self.cur_state[0].t, self.curvature_factor, self.slip_factor ,self.smooth_factor, self.accel_limit, float(restricted_steer_rate) ,self.ff_angle_factor, self.ff_rate_factor, self.pCost, self.lCost, self.rCost, self.hCost, self.srCost, steer_motor, float(driver_torque), \
      self.angle_rate_count, self.angle_rate_desired, self.avg_angle_rate, future_angle_steers, float(angle_rate), self.steer_zero_crossing, self.center_angle, angle_steers, self.angle_steers_des, angle_offset, \
      self.angle_steers_des_mpc, CP.steerRatio, CP.steerKf, CP.steerKpV[0], CP.steerKiV[0], CP.steerRateCost, PL.PP.l_prob, \
      PL.PP.r_prob, PL.PP.c_prob, PL.PP.p_prob, self.l_poly[0], self.l_poly[1], self.l_poly[2], self.l_poly[3], self.r_poly[0], self.r_poly[1], self.r_poly[2], self.r_poly[3], \
      self.p_poly[0], self.p_poly[1], self.p_poly[2], self.p_poly[3], PL.PP.c_poly[0], PL.PP.c_poly[1], PL.PP.c_poly[2], PL.PP.c_poly[3], PL.PP.d_poly[0], PL.PP.d_poly[1], \
      PL.PP.d_poly[2], PL.PP.lane_width, PL.PP.lane_width_estimate, PL.PP.lane_width_certainty, v_ego, self.pid.p, self.pid.i, self.pid.f, int(time.time() * 100) * 10000000))

    self.sat_flag = self.pid.saturated
    return output_steer, float(self.angle_steers_des)
