import math, logging, os
from enum import Enum
from inspect import signature
from . import tmc


import io
from typing import Callable, Optional


# Autotune config parameters
TUNING_GOAL = 'auto'
EXTRA_HYSTERESIS = 0
TBL = 1
TOFF = 0
SGT = 1
SG4_THRS = 40
VOLTAGE = 24.0
OVERVOLTAGE_VTH = None

# Generic tuning parameters
COOLSTEP_THRS_FACTOR = 0.75
FULLSTEP_THRS_FACTOR = 1.2
MULTISTEP_FILT = True

# 2240-specific parameters
SLOPE_CONTROL = 3

# PWM parameters
PWM_AUTOSCALE = True # Setup pwm autoscale even if we won't use PWM, because it
                     # gives more data about the motor and is needed for CoolStep.
PWM_AUTOGRAD = True
PWM_REG = 15
PWM_LIM = 4

# SpreadCycle parameters
TPFD = 0

# CoolStep parameters
FAST_STANDSTILL = True
SMALL_HYSTERESIS = False
SEMIN = 2
SEMAX = 4
SEUP = 3
SEDN = 2
SEIMIN = 1 # If we drop to 1/4 current, high accels don't work right
SFILT = 0
IHOLDDELAY = 12
IRUNDELAY = 0

# High speed parameters
VHIGHFS = False
VHIGHCHM = False # Even though we are fullstepping, we want SpreadCycle control


TRINAMIC_DRIVERS = ["tmc2130", "tmc2208", "tmc2209", "tmc2240", "tmc2660", "tmc5160"]
PWM_FREQ_TARGETS = {"tmc2130": 55e3,
                    "tmc2208": 55e3,
                    "tmc2209": 55e3,
                    "tmc2240": 20e3, # 2240s run very hot at high frequencies
                    "tmc2660": 55e3,
                    "tmc5160": 55e3}


AUTO_PERFORMANCE_MOTORS = {'stepper_x', 'stepper_y', 'stepper_x1', 'stepper_y1', 'stepper_a', 'stepper_b', 'stepper_c'}


ST_COMMANDS = {
    'AUTOTUNE_TMC': (
        'Apply autotuning configuration to TMC stepper driver'
    ),
    'TAT_AUTOTUNE': (
        'A wrapper for AUTOTUNE_TMC'
    )
}



class TuningGoal(str, Enum):
    AUTO = "auto" # This is the default: automatically choose SILENT for Z and PERFORMANCE for X/Y
    AUTOSWITCH = "autoswitch" # Experimental mode that use StealthChop at low speed and switch to SpreadCycle when needed
    SILENT = "silent" # StealthChop at all speeds
    PERFORMANCE = "performance" # SpreadCycle at all speeds

class AutotuneTMC:
    def __init__(self, config):
        
        self._config = config
        self._printer = config.get_printer()

        # Load motor database
        pconfig = self._printer.lookup_object('configfile')
        dirname = os.path.dirname(os.path.realpath(__file__))
        filename = os.path.join(dirname, 'motor_database.cfg')
        try:
            motor_db = pconfig.read_config(filename)
        except Exception:
            raise config.error("Cannot load config '%s'" % (filename,))
        for motor in motor_db.get_prefix_sections(''):
            self._printer.load_object(motor_db, motor.get_name())

        
        # Register the console print output callback to the corresponding Klipper function
        gcode = self._printer.lookup_object('gcode')
        ConsoleOutput.register_output_callback(gcode.respond_info)


        # Now find our stepper and driver in the running Klipper config
        self.name = config.get_name().split(None, 1)[-1]
        if not config.has_section(self.name):
            raise config.error(
                "Could not find stepper config section '[%s]' required by TMC autotuning"
                % (self.name))
        self.tmc_section = None
        for driver in TRINAMIC_DRIVERS:
            driver_name = "%s %s" % (driver, self.name)
            if config.has_section(driver_name):
                self.tmc_section = config.getsection(driver_name)
                self.driver_name = driver_name
                self.driver_type = driver
                break
        if self.tmc_section is None:
            raise config.error(
                "Could not find any TMC driver config section for '%s' required by TMC autotuning"
                % (self.name))
        # TMCtstepHelper may have two signatures, let's pick an implementation
        if 'pstepper' in signature(tmc.TMCtstepHelper).parameters:
            self._set_driver_velocity_field = self._set_driver_velocity_field_new
        else:
            self._set_driver_velocity_field = self._set_driver_velocity_field_old
        # AutotuneTMC config parameters
        self.motor = config.get('motor')
        self.motor_name = "motor_constants " + self.motor
        tgoal = config.get('tuning_goal', default=TUNING_GOAL).lower()
        try:
            self.tuning_goal = TuningGoal(tgoal)
        except ValueError:
            raise config.error(
                "Tuning goal '%s' is invalid for TMC autotuning"
                % (tgoal))
        self.auto_silent = False # Auto silent off by default
        self.tmc_object=None # look this up at connect time
        self.tmc_cmdhelper=None # Ditto
        self.tmc_init_registers=None # Ditto
        self.run_current = 0.0
        self.fclk = None
        self.motor_object = None
        self.extra_hysteresis = config.getint('extra_hysteresis', default=EXTRA_HYSTERESIS,
                                              minval=0, maxval=8)
        self.tbl = config.getint('tbl', default=TBL, minval=0, maxval=3)
        self.toff = config.getint('toff', default=None, minval=1, maxval=15)
        self.tpfd = config.getint('tpfd', default=None, minval=0, maxval=15)
        self.sgt = config.getint('sgt', default=SGT, minval=-64, maxval=63)
        self.sg4_thrs = config.getint('sg4_thrs', default=SG4_THRS, minval=0, maxval=255)
        self.voltage = config.getfloat('voltage', default=VOLTAGE, minval=0.0, maxval=60.0)
        self.overvoltage_vth = config.getfloat('overvoltage_vth', default=OVERVOLTAGE_VTH,
                                              minval=0.0, maxval=60.0)
        self.pwm_freq_target = config.getfloat('pwm_freq_target',
                                               default=PWM_FREQ_TARGETS[self.driver_type],
                                               minval=10e3, maxval=100e3)
        self._printer.register_event_handler("klippy:connect",
                                            self.handle_connect)
        self._printer.register_event_handler("klippy:ready",
                                            self.handle_ready)
        # Register command
        self._register_commands()


    def _register_commands(self) -> None:
        gcode = self._printer.lookup_object("gcode")
        gcode.register_mux_command("AUTOTUNE_TMC", "STEPPER", self.name,
                                   self.cmd_AUTOTUNE_TMC,
                                   desc=self.cmd_AUTOTUNE_TMC_help)

        # Then, a hack to inject the macros into Klipper's config system in order to show them in the web
        # interfaces. This is not a good way to do it, but it's the only way to do it for now to get
        # a good user experience while using Shake&Tune (it's indeed easier to just click a macro button)

        configfile = self._printer.lookup_object('configfile')
        dirname = os.path.dirname(os.path.realpath(__file__))
        filename = os.path.join(dirname, 'dummy_macros.cfg')
        try:
            dummy_macros_cfg = configfile.read_config(filename)
        except Exception as err:
            raise self._config.error(f'Cannot load TAT dummy macro {filename}') from err

        for gcode_macro in dummy_macros_cfg.get_prefix_sections('gcode_macro '):
            gcode_macro_name = gcode_macro.get_name()

            # Replace the dummy description by the one from ST_COMMANDS (to avoid code duplication and define it in only one place)
            command = gcode_macro_name.split(' ', 1)[1]
            description = ST_COMMANDS.get(command, 'TAT macro')
            gcode_macro.fileconfig.set(gcode_macro_name, 'description', description)

            # Add the section to the Klipper configuration object with all its options
            if not self._config.fileconfig.has_section(gcode_macro_name.lower()):
                self._config.fileconfig.add_section(gcode_macro_name.lower())
            for option in gcode_macro.fileconfig.options(gcode_macro_name):
                value = gcode_macro.fileconfig.get(gcode_macro_name, option)
                self._config.fileconfig.set(gcode_macro_name.lower(), option, value)
                # Small trick to ensure the new injected sections are considered valid by Klipper config system
                self._config.access_tracking[(gcode_macro_name.lower(), option.lower())] = 1

            # Finally, load the section within the printer objects
            self._printer.load_object(self._config, gcode_macro_name.lower())





    def handle_connect(self):
        self.tmc_object = self._printer.lookup_object(self.driver_name)
        # The cmdhelper itself isn't a member... but we can still get to it.
        self.tmc_cmdhelper = self.tmc_object.get_status.__self__
        try:
            motor = self._printer.lookup_object(self.motor_name)
        except Exception:
            raise self._printer.config_error(
                "Could not find motor definition '[%s]' required by TMC autotuning. "
                "It is not part of the database, please define it in your config!"
                % (self.motor_name))
        if self.tuning_goal == TuningGoal.AUTO:
            # Very small motors may not run in silent mode.
            self.auto_silent = self.name not in AUTO_PERFORMANCE_MOTORS and motor.T > 0.3
            self.tuning_goal = TuningGoal.SILENT if self.auto_silent else TuningGoal.PERFORMANCE
        self.motor_object = self._printer.lookup_object(self.motor_name)
        #self.tune_driver()

    def handle_ready(self):
      # klippy:ready handlers are limited in what they may do. Communicating with a MCU
      # will pause the reactor and is thus forbidden. That code has to run outside of the event handler.
      self._printer.reactor.register_callback(self._handle_ready_deferred)

    def _handle_ready_deferred(self, eventtime):
        if self.tmc_init_registers is not None:
            self.tmc_init_registers(print_time=print_time)
        try:
            self.fclk = self.tmc_object.mcu_tmc.get_tmc_frequency()
        except AttributeError:
            pass
        if self.fclk is None:
            self.fclk = 12.5e6
        self.tune_driver()




    # AUTOTUNE_TMC command

    cmd_AUTOTUNE_TMC_help = "Apply autotuning configuration to TMC stepper driver"
    def cmd_AUTOTUNE_TMC(self, gcmd):
        logging.info("AUTOTUNE_TMC %s", self.name)
        tgoal = gcmd.get('TUNING_GOAL', None)
        verbose = gcmd.get('TUNING_GOAL', 1)
        
        if verbose == 1:
            ConsoleOutput.print("Starting auto tuning")
        

        # setting tuning goal
        if tgoal is not None:
            try:
                self.tuning_goal = TuningGoal(tgoal)
            except ValueError:
                # TODO: add some logging/error here in case the tuning_goal doesn't exist
                pass
            if self.tuning_goal == TuningGoal.AUTO:
                self.tuning_goal = TuningGoal.SILENT if self.auto_silent else TuningGoal.PERFORMANCE
        # printing tuning goal
            
        

        # setting extra_hysteresis
        extra_hysteresis = gcmd.get_int('EXTRA_HYSTERESIS', None)
        if extra_hysteresis is not None:
            if extra_hysteresis >= 0 or extra_hysteresis <= 8:
                self.extra_hysteresis = extra_hysteresis

        # setting tbl
        tbl = gcmd.get_int('TBL', None)
        if tbl is not None:
            if tbl >= 0 or tbl <= 3:
                self.tbl = tbl  

        # setting toff
        toff = gcmd.get_int('TOFF', None)
        if toff is not None:
            if toff >= 1 or toff <= 15:
                self.toff = toff

        # setting tpfd
        tpfd = gcmd.get_int('TPFD', None)
        if tpfd is not None:
            if tpfd >= 0 or tpfd <= 15:
                self.tpfd = tpfd

        # setting sgt
        sgt = gcmd.get_int('SGT', None)
        if sgt is not None:
            if sgt >= -64 or sgt <= 63:
                self.sgt = sgt

        # setting sg4_thrs
        sg4_thrs = gcmd.get_int('SG4_THRS', None)
        if sg4_thrs is not None:
            if sg4_thrs >= 0 or sg4_thrs <= 255:
                self.sg4_thrs = sg4_thrs
        
        # setting voltage
        voltage = gcmd.get_float('VOLTAGE', None)
        if voltage is not None:
            if voltage >= 0.0 or voltage <= 60.0:
                self.voltage = voltage

        # setting overvoltage_vth        
        overvoltage_vth = gcmd.get_float('OVERVOLTAGE_VTH', None)
        if overvoltage_vth is not None:
            if overvoltage_vth >= 0.0 or overvoltage_vth <= 60.0:
                self.overvoltage_vth = overvoltage_vth

        
        self.tune_driver()


        if verbose == 1:
            ConsoleOutput.print(f"Stepper: {self.name}\n" + \
                                f"Tuninggoal: {self.tuning_goal}\n" + \
                                f"current: {self.run_current}\n" + \
                                f"Voltage: {self.voltage}\n" + \
                                f"Extra hysteresis:: {self.tuning_goal}\n" + \
                                f"Tpfd: {self.tpfd}-tbl: {self.tbl}-toff: {self.toff}\n" + \
                                f"Sgt: {self.sgt} - sg4_thrs: {self.sg4_thrs}\n" + \
                                f"Overvoltage vth: {self.overvoltage_vth}\n" + \
                                f"Pwm freq: {self.pwm_freq}\n" + \
                                f"Max Pwm speed: {round(self.maxpwmrps, 2)}rps {round(self.vmaxpwm, 1)}mm/s\n" 

                                )
            

            
            #ConsoleOutput.print("tuning goal: %s" % (self.tuning_goal))
            #ConsoleOutput.print("extra hysteresis: %s" % (self.extra_hysteresis))
            #ConsoleOutput.print("tpfd: %.2f" % (self.tpfd))
            #ConsoleOutput.print("tbl: %.2f" % (self.tbl))
            #ConsoleOutput.print("toff: %.2f" % (self.toff))
            #ConsoleOutput.print("sgt: %s" % (self.sgt))
            #ConsoleOutput.print("sg4_thrs: %s" % (self.sg4_thrs))
            #ConsoleOutput.print("voltage: %s" % (self.voltage))
            #ConsoleOutput.print("overvoltage_vth: %s" % (self.overvoltage_vth))

            #ConsoleOutput.print("run_current: %s" % (self.run_current))
            #ConsoleOutput.print("pwm_freq: %s" % (self.pwm_freq))
            



    def tune_driver(self, print_time=None):

        # setting the current
        _currents = self.tmc_cmdhelper.current_helper.get_current()
        self.run_current = _currents[0]

        # setting the pwm freq
        self._set_pwmfreq()

        # setting up spreadcycle
        self._setup_spreadcycle()

        # setting up hysteresis
        self._set_hysteresis(self.run_current)

        # setting up sg4thrs
        self._set_sg4thrs()

        # setting up motor
        motor = self.motor_object


        # Speed at which we run out of PWM control and should switch to fullstep in rps
        self.maxpwmrps = motor.maxpwmrps(volts=self.voltage, current=self.run_current)
        rdist, _ = self.tmc_cmdhelper.stepper.get_rotation_distance()
        # Speed at which we run out of PWM control and should switch to fullstep in mm/s
        self.vmaxpwm = self.maxpwmrps * rdist
        logging.info("autotune_tmc using max PWM speed %f", self.vmaxpwm)


        if self.overvoltage_vth is not None:
            vth = int((self.overvoltage_vth / 0.009732))
            self._set_driver_field('overvoltage_vth', vth)
        coolthrs = COOLSTEP_THRS_FACTOR * rdist
        self._setup_pwm(self.tuning_goal, self._pwmthrs(self.vmaxpwm, coolthrs))
        # One revolution every two seconds is about as slow as coolstep can go
        self._setup_coolstep(coolthrs)
        self._setup_highspeed(FULLSTEP_THRS_FACTOR * self.vmaxpwm)
        self._set_driver_field('multistep_filt', MULTISTEP_FILT)
        # Cool down 2240s
        self._set_driver_field('slope_control', SLOPE_CONTROL)




    def _set_driver_field(self, field, arg):
        tmco = self.tmc_object
        register = tmco.fields.lookup_register(field, None)
        # Just bail if the field doesn't exist.
        if register is None:
            return
        logging.info("autotune_tmc set %s %s=%s", self.name, field, repr(arg))
        val = tmco.fields.set_field(field, arg)
        tmco.mcu_tmc.set_register(register, val, None)

    def _set_driver_velocity_field_new(self, field, velocity):
        tmco = self.tmc_object
        register = tmco.fields.lookup_register(field, None)
        # Just bail if the field doesn't exist.
        if register is None:
            return
        arg = tmc.TMCtstepHelper(tmco.mcu_tmc, velocity,
                                 pstepper=self.tmc_cmdhelper.stepper)
        logging.info("autotune_tmc set %s %s=%s(%s)",
                     self.name, field, repr(arg), repr(velocity))
        tmco.fields.set_field(field, arg)

    def _set_driver_velocity_field_old(self, field, velocity):
        tmco = self.tmc_object
        register = tmco.fields.lookup_register(field, None)
        # Just bail if the field doesn't exist.
        if register is None:
            return
        step_dist = self.tmc_cmdhelper.stepper.get_step_dist()
        mres = tmco.fields.get_field("mres")
        arg = tmc.TMCtstepHelper(step_dist, mres, self.fclk, velocity)
        logging.info("autotune_tmc set %s %s=%s(%s)",
                     self.name, field, repr(arg), repr(velocity))
        tmco.fields.set_field(field, arg)

    def _set_pwmfreq(self):
        # calculate the highest pwm_freq that gives less than 50 kHz chopping
        self.pwm_freq = next((i
                         for i in [(3, 2./410),
                                   (2, 2./512),
                                   (1, 2./683),
                                   (0, 2./1024),
                                   (0, 0.) # Default case, just do the best we can.
                                   ]
                         if self.fclk*i[1] < self.pwm_freq_target))[0]
        
        self._set_driver_field('pwm_freq', self.pwm_freq)

    def _set_hysteresis(self, run_current):
        hstrt, hend = self.motor_object.hysteresis(
            volts=self.voltage,
            current=run_current,
            tbl=self.tbl,
            toff=self.toff,
            fclk=self.fclk,
            extra=self.extra_hysteresis)
        self._set_driver_field('hstrt', hstrt)
        self._set_driver_field('hend', hend)

    def _set_sg4thrs(self):
        if self.tmc_object.fields.lookup_register("sg4_thrs", None) is not None:
            # we have SG4
            self._set_driver_field('sg4_thrs', self.sg4_thrs)
            self._set_driver_field('sg4_filt_en', True)
        elif self.tmc_object.fields.lookup_register("sgthrs", None) is not None:
            # With SG4 on 2209, pwmthrs should be greater than coolthrs
            self._set_driver_field('sgthrs', self.sg4_thrs)
        else:
            # We do not have SG4
            pass

    def _pwmthrs(self, vmaxpwm, coolthrs):
        if self.tmc_object.fields.lookup_register("sg4_thrs", None) is not None:
            # we have SG4
            # 2240 doesn't care about pwmthrs vs coolthrs ordering, but this is desirable
            return max(0.2 * vmaxpwm, 1.125 * coolthrs)
        elif self.tmc_object.fields.lookup_register("sgthrs", None) is not None:
            # With SG4 on 2209, pwmthrs should be greater than coolthrs
            return max(0.2 * vmaxpwm, 1.125 * coolthrs)
        else:
            # We do not have SG4, so this makes the world safe for
            # sensorless homing in the presence of CoolStep
            # return 0.5 * coolthrs
            return 0.5 * vmaxpwm

    def _setup_pwm(self, tgoal, pwmthrs):
        motor = self.motor_object
        pwmgrad = motor.pwmgrad(volts=self.voltage, fclk=self.fclk)
        pwmofs = motor.pwmofs(volts=self.voltage, current=self.run_current)
        self._set_driver_field('pwm_autoscale', PWM_AUTOSCALE)
        self._set_driver_field('pwm_autograd', PWM_AUTOGRAD)
        self._set_driver_field('pwm_grad', pwmgrad)
        self._set_driver_field('pwm_ofs', pwmofs)
        self._set_driver_field('pwm_reg', PWM_REG)
        self._set_driver_field('pwm_lim', PWM_LIM)
        if tgoal == TuningGoal.AUTOSWITCH:
            self._set_driver_velocity_field('tpwmthrs', pwmthrs)
            self._set_driver_field('en_pwm_mode', True)
            self._set_driver_field('en_spreadcycle', False) # TMC2208 use en_spreadcycle instead of en_pwm_mode
        elif tgoal == TuningGoal.SILENT:
            self._set_driver_field('tpwmthrs', 0)
            self._set_driver_field('en_pwm_mode', True)
            self._set_driver_field('en_spreadcycle', False) # TMC2208 use en_spreadcycle instead of en_pwm_mode
        elif tgoal == TuningGoal.PERFORMANCE:
            self._set_driver_field('tpwmthrs', 0xfffff)
            self._set_driver_field('en_pwm_mode', False)
            self._set_driver_field('en_spreadcycle', True) # TMC2208 use en_spreadcycle instead of en_pwm_mode

    def _setup_spreadcycle(self):
        ncycles = int(math.ceil(self.fclk / self.pwm_freq_target))
        sdcycles = ncycles / 4
        if self.toff == 0 or self.toff is None:
            # About half the cycle should be taken by the two slow decay cycles
            self.toff = max(min(int(math.ceil(max(sdcycles - 24, 0) / 32)), 15), 1)

        if self.tbl is None:
            self.tbl = TBL

        if self.toff == 1 and self.tbl == 0:
            # blank time of 16 cycles will not work in this case
            self.tbl = 1

        pfdcycles = ncycles - (24 + 32 * self.toff) * 2 - [16, 34, 36, 54][self.tbl]
        if self.tpfd is None:
            self.tpfd = max(0, min(15, int(math.ceil(pfdcycles / 128))))

        logging.info("autotune_tmc %s ncycles=%d pfdcycles=%d", self.name, ncycles, pfdcycles)

        self._set_driver_field('tpfd', self.tpfd)
        self._set_driver_field('tbl', self.tbl)
        self._set_driver_field('toff', self.toff)

    def _setup_coolstep(self, coolthrs):
        self._set_driver_velocity_field('tcoolthrs', coolthrs)
        self._set_driver_field('sgt', self.sgt)
        self._set_driver_field('fast_standstill', FAST_STANDSTILL)
        self._set_driver_field('small_hysteresis', SMALL_HYSTERESIS)
        self._set_driver_field('semin', SEMIN)
        self._set_driver_field('semax', SEMAX)
        self._set_driver_field('seup', SEUP)
        self._set_driver_field('sedn', SEDN)
        self._set_driver_field('seimin', SEIMIN)
        self._set_driver_field('sfilt', SFILT)
        self._set_driver_field('iholddelay', IHOLDDELAY)
        self._set_driver_field('irundelay', IRUNDELAY)

    def _setup_highspeed(self, vhigh):
        self._set_driver_velocity_field('thigh', vhigh)
        self._set_driver_field('vhighfs', VHIGHFS)
        self._set_driver_field('vhighchm', VHIGHCHM)


def load_config_prefix(config):
    return AutotuneTMC(config)




# utils :

# this class is a direct copy of Shake&Tune 


# Shake&Tune: 3D printer analysis tools
#
# Copyright (C) 2024 Félix Boisselier <felix@fboisselier.fr> (Frix_x on Discord)
# Licensed under the GNU General Public License v3.0 (GPL-3.0)
#
# File: console_output.py
# Description: Defines the ConsoleOutput class for printing output to stdout or an alternative
#              callback function, such as the Klipper console.

class ConsoleOutput:
    """
    Print output to stdout or to an alternative like the Klipper console through a callback
    """

    _output_func: Optional[Callable[[str], None]] = None

    @classmethod
    def register_output_callback(cls, output_func: Optional[Callable[[str], None]]):
        cls._output_func = output_func

    @classmethod
    def print(cls, *args, **kwargs):
        if not cls._output_func:
            print(*args, **kwargs)
            return

        with io.StringIO() as mem_output:
            print(*args, file=mem_output, **kwargs)
            cls._output_func(mem_output.getvalue())