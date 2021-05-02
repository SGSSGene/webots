# Copyright 1996-2021 Cyberbotics Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from gamestate import GameState
from field import Field
from controller import Supervisor, AnsiCodes, Node

import copy
import json
import math
import os
import random
import socket
import subprocess
import sys
import time
import transforms3d

from types import SimpleNamespace

OUTSIDE_TURF_TIMEOUT = 20                 # a player outside the turf for more than 20 seconds gets a removal penalty
INACTIVE_GOAL_KEEPER_TIMEOUT = 20         # a goal keeper is penalized if inactive for 20 seconds while the ball is in goal area
DROPPED_BALL_TIMEOUT = 120                # wait 2 simulated minutes if the ball doesn't move before starting dropped ball
SIMULATED_TIME_INTERRUPTION_PHASE_0 = 10  # waiting time of 10 simulated seconds in phase 0 of interruption
SIMULATED_TIME_INTERRUPTION_PHASE_1 = 30  # waiting time of 30 simulated seconds in phase 1 of interruption
SIMULATED_TIME_BEFORE_PLAY_STATE = 5      # wait 5 simulated seconds in SET state before sending the PLAY state
HALF_TIME_BREAK_SIMULATED_DURATION = 15   # the half-time break lasts 15 simulated seconds
REAL_TIME_BEFORE_FIRST_READY_STATE = 120  # wait 2 real minutes before sending the first READY state
IN_PLAY_TIMEOUT = 10                      # time after which the ball is considered in play even if it was not kicked
FALLEN_TIMEOUT = 20                       # if a robot is down (fallen) for more than this amount of time, it gets penalized
GOAL_KEEPER_BALL_HOLDING_TIMEOUT = 6      # a goal keeper may hold the ball up to 6 seconds on the ground
PLAYER_BALL_HOLDING_TIMEOUT = 1           # a field player may hold the ball up to 1 second
HAND_BALL_HOLDING_TIMEOUT = 10            # a player throwing in or a goal keeper may hold the ball up to 10 seconds in hands
BALL_IN_PLAY_MOVE = 0.05                  # the ball must move 5 cm after interruption or kickoff to be considered in play
FOUL_PUSHING_TIME = 1                     # 1 second
FOUL_PUSHING_PERIOD = 2                   # 2 seconds
FOUL_VINCITY_DISTANCE = 2                 # 2 meters
FOUL_DISTANCE_THRESHOLD = 0.1             # 0.1 meter
FOUL_SPEED_THRESHOLD = 0.2                # 0.2 m/s
FOUL_DIRECTION_THRESHOLD = math.pi / 6    # 30 degrees
LINE_WIDTH = 0.05                         # width of the white lines on the soccer field
GOAL_WIDTH = 2.6                          # width of the goal
RED_COLOR = 0xd62929                      # red team color used for the display
BLUE_COLOR = 0x2943d6                     # blue team color used for the display

# game interruptions requiring a free kick procedure
GAME_INTERRUPTIONS = {
    'DIRECT_FREEKICK': 'direct free kick',
    'INDIRECT_FREEKICK': 'indirect free kick',
    'PENALTYKICK': 'penalty kick',
    'CORNERKICK': 'corner kick',
    'GOALKICK': 'goal kick',
    'THROWIN': 'throw in'}

LINE_HALF_WIDTH = LINE_WIDTH / 2
GOAL_HALF_WIDTH = GOAL_WIDTH / 2

global supervisor, game, red_team, blue_team, log_file, time_count


def log(message, type):
    console_message = f'{AnsiCodes.YELLOW_FOREGROUND}{AnsiCodes.BOLD}{message}{AnsiCodes.RESET}' if type == 'Warning' \
                      else message
    print(console_message, file=sys.stderr if type == 'Error' else sys.stdout)
    if log_file:
        real_time = int(1000 * (time.time() - log.start_time)) / 1000
        log_file.write(f'[{real_time:08.3f}|{time_count / 1000:08.3f}] {type}: {message}\n')  # log real and virtual times


log.start_time = time.time()


def info(message):
    log(message, 'Info')


def warning(message):
    log(message, 'Warning')


def error(message):
    log(message, 'Error')


def toss_a_coin_if_needed(attribute):  # attribute should be either "side_left" or "kickoff"
    # If game.json contains such an attribute, use it to determine field side and kick-off
    # Supported values are "red", "blue" and "random". Default value is "random".
    if hasattr(game, attribute):
        if getattr(game, attribute) == 'red':
            setattr(game, attribute, game.red.id)
        elif getattr(game, attribute) == 'blue':
            setattr(game, attribute, game.blue.id)
        elif getattr(game, attribute) != 'random':
            error(f'Unsupported value for "{attribute}" in game.json file: {getattr(game, attribute)}, using "random".')
            setattr(game, attribute, 'random')
    else:
        setattr(game, attribute, 'random')
    if getattr(game, attribute) == 'random':  # toss a coin to determine a random team
        setattr(game, attribute, game.red.id if bool(random.getrandbits(1)) else game.blue.id)


def spawn_team(team, red_on_right, children):
    color = team['color']
    for number in team['players']:
        model = team['players'][number]['proto']
        n = int(number) - 1
        port = game.red.ports[n] if color == 'red' else game.blue.ports[n]
        if red_on_right:  # symmetry with respect to the central line of the field
            flip_poses(team['players'][number])
        defname = color.upper() + '_PLAYER_' + number
        halfTimeStartingTranslation = team['players'][number]['halfTimeStartingPose']['translation']
        halfTimeStartingRotation = team['players'][number]['halfTimeStartingPose']['rotation']
        string = f'DEF {defname} {model}{{name "{color} player {number}" translation ' + \
            f'{halfTimeStartingTranslation[0]} {halfTimeStartingTranslation[1]} {halfTimeStartingTranslation[2]} rotation ' + \
            f'{halfTimeStartingRotation[0]} {halfTimeStartingRotation[1]} {halfTimeStartingRotation[2]} ' + \
            f'{halfTimeStartingRotation[3]} controllerArgs ["{port}"'
        hosts = game.red.hosts if color == 'red' else game.blue.hosts
        for host in hosts:
            string += f', "{host}"'
        string += '] }}'
        children.importMFNodeFromString(-1, string)
        team['players'][number]['robot'] = supervisor.getFromDef(defname)
        info(f'Spawned {defname} {model} on port {port} at halfTimeStartingPose: translation (' +
             f'{halfTimeStartingTranslation[0]} {halfTimeStartingTranslation[1]} {halfTimeStartingTranslation[2]}), ' +
             f'rotation ({halfTimeStartingRotation[0]} {halfTimeStartingRotation[1]} {halfTimeStartingRotation[2]} ' +
             f'{halfTimeStartingRotation[3]}).')


def format_time(s):
    seconds = str(s % 60)
    minutes = str(int(s / 60))
    if len(minutes) == 1:
        minutes = '0' + minutes
    if len(seconds) == 1:
        seconds = '0' + seconds
    return minutes + ':' + seconds


def update_time_display():
    if game.state:
        s = game.state.seconds_remaining
        if s < 0:
            s = -s
            sign = '-'
        else:
            sign = ' '
        value = format_time(s)
    else:
        sign = ' '
        value = '--:--'
    supervisor.setLabel(5, sign + value, game.overlay_x, game.overlay_y, game.font_size, 0x000000, 0.2, game.font)


def update_state_display():
    if game.state:
        state = game.state.game_state[6:]
        if state == 'READY' or state == 'SET':  # kickoff
            color = RED_COLOR if game.kickoff == game.red.id else BLUE_COLOR
        elif game.interruption_team is not None:  # interruption
            color = RED_COLOR if game.interruption_team == game.red.id else BLUE_COLOR
        else:
            color = 0x000000
        sr = IN_PLAY_TIMEOUT - game.interruption_seconds + game.state.seconds_remaining \
            if game.interruption_seconds is not None \
            else game.state.secondary_seconds_remaining
        if sr > 0:
            if game.interruption is None:  # kickoff
                color = RED_COLOR if game.kickoff == game.red.id else BLUE_COLOR
                state = 'PLAY' if state == 'PLAYING' else 'READY'
            else:  # interruption
                state = 'PLAY' if state == 'PLAYING' and game.interruption_seconds is not None else 'READY'
            state += ' ' + format_time(sr)
        elif game.interruption is not None:
            state = game.interruption
    else:
        state = ''
        color = 0x000000
    supervisor.setLabel(6, ' ' * 41 + state, game.overlay_x, game.overlay_y, game.font_size, color, 0.2, game.font)


def update_score_display():
    if game.state:
        red = 0 if game.state.teams[0].team_color == 'RED' else 1
        blue = 1 if red == 0 else 0
        red_score = str(game.state.teams[red].score)
        blue_score = str(game.state.teams[blue].score)
    else:
        red_score = '0'
        blue_score = '0'
    if game.side_left == game.blue.id:
        red_score = ' ' * 24 + red_score
        offset = 21 if len(blue_score) == 2 else 22
        blue_score = ' ' * offset + blue_score
    else:
        blue_score = ' ' * 24 + blue_score
        offset = 21 if len(red_score) == 2 else 22
        red_score = ' ' * offset + red_score
    supervisor.setLabel(7, red_score, game.overlay_x, game.overlay_y, game.font_size, 0x000000, 0.2, game.font)
    supervisor.setLabel(8, blue_score, game.overlay_x, game.overlay_y, game.font_size, 0x000000, 0.2, game.font)


def update_team_display():
    n = len(red_team['name'])
    red_team_name = ' ' * 27 + red_team['name'] if game.side_left == game.blue.id else (20 - n) * ' ' + red_team['name']
    n = len(blue_team['name'])
    blue_team_name = (20 - n) * ' ' + blue_team['name'] if game.side_left == game.blue.id else ' ' * 27 + blue_team['name']
    supervisor.setLabel(3, red_team_name, game.overlay_x, game.overlay_y, game.font_size, RED_COLOR, 0.2, game.font)
    supervisor.setLabel(4, blue_team_name, game.overlay_x, game.overlay_y, game.font_size, BLUE_COLOR, 0.2, game.font)
    update_score_display()


def setup_display():
    black = 0x000000
    white = 0xffffff
    transparency = 0.2
    x = game.overlay_x
    y = game.overlay_y
    size = game.font_size
    font = game.font
    # default background
    supervisor.setLabel(0, '█' * 7 + ' ' * 14 + '█' * 5 + 14 * ' ' + '█' * 14, x, y, size, white, transparency, font)
    # team name background
    supervisor.setLabel(1, ' ' * 7 + '█' * 14 + ' ' * 5 + 14 * '█', x, y, size, white, transparency * 2, font)
    supervisor.setLabel(2, ' ' * 23 + '-', x, y, size, black, transparency, font)
    update_team_display()
    update_time_display()
    update_state_display()


def team_index(color):
    id = game.red.id if color == 'red' else game.blue.id
    index = 0 if game.state.teams[0].team_number == id else 1
    return index


def game_controller_receive():
    try:
        data, peer = game.udp.recvfrom(GameState.sizeof())
    except BlockingIOError:
        return
    except Exception as e:
        error(f'UDP input failure: {e}')
        data = None
        pass
    if not data:
        error('No UDP data received')
        return
    previous_seconds_remaining = game.state.seconds_remaining if game.state else 0
    previous_secondary_seconds_remaining = game.state.secondary_seconds_remaining if game.state else 0
    previous_state = game.state.game_state if game.state else None
    if game.state:
        red = 0 if game.state.teams[0].team_color == 'RED' else 1
        blue = 1 if red == 0 else 0
        previous_red_score = game.state.teams[red].score
        previous_blue_score = game.state.teams[blue].score
    else:
        previous_red_score = 0
        previous_blue_score = 0

    game.state = GameState.parse(data)

    # Fixes bug of GameController, possibly fixed in https://github.com/RoboCup-Humanoid-TC/GameController/pull/141
    # FIXME: if these errors don't how up any more on the long term, we should remove this test
    if game.state.seconds_remaining == 600 and previous_seconds_remaining != 600 and previous_seconds_remaining != 0:
        game.state.seconds_remaining = previous_seconds_remaining
        error(f'GameController sent wrong 600 seconds remaining, using previous value: {previous_seconds_remaining}.')
    if game.state.secondary_seconds_remaining == 0 and previous_secondary_seconds_remaining > 2:
        # It sometimes happens in fast mode that the GameController drops secondary seconds from 2 to 0 (hence the > 2 test)
        # This seems acceptable and is ignored by the test, but on a more powerful computer it may drop from 3 to 0
        # and display the error.
        game.state.secondary_seconds_remaining = previous_secondary_seconds_remaining
        error('GameController sent wrong 0 secondary seconds remaining, using previous value: '
              f'{previous_secondary_seconds_remaining}.')

    if previous_state != game.state.game_state:
        if game.wait_for_state is not None:
            if 'STATE_' + game.wait_for_state != game.state.game_state:
                error(f'Received unexpected state from GameController: {game.state.game_state} ' +
                      f'while expecting {game.wait_for_state}')
            else:
                game.wait_for_state = None
        info(f'New state received from GameController: {game.state.game_state}.')
    if game.state.game_state == 'STATE_PLAYING' and \
       game.state.secondary_seconds_remaining == 0 and previous_secondary_seconds_remaining > 0:
        if game.in_play is None and game.phase == 'KICKOFF':
            info('Ball in play, can be touched by any player (10 seconds elapsed after kickoff).')
            game.in_play = time_count
    if previous_state != game.state.game_state or \
       previous_secondary_seconds_remaining != game.state.secondary_seconds_remaining or \
       game.state.seconds_remaining <= 0:
        update_state_display()
    if previous_seconds_remaining != game.state.seconds_remaining:
        if game.interruption_seconds is not None:
            if game.interruption_seconds - game.state.seconds_remaining > IN_PLAY_TIMEOUT:
                if game.in_play is None:
                    info('Ball in play, can be touched by any player (10 seconds elapsed).')
                    game.in_play = time_count
                    game.interruption = None
                    game.interruption_step = None
                    game.interruption_step_time = 0
                    game.interruption_team = None
                    game.interruption_seconds = None
            update_state_display()
        update_time_display()
    red = 0 if game.state.teams[0].team_color == 'RED' else 1
    blue = 1 if red == 0 else 0
    if previous_red_score != game.state.teams[red].score or \
       previous_blue_score != game.state.teams[blue].score:
        update_score_display()
    # print(game.state.game_state)
    secondary_state = game.state.secondary_state
    secondary_state_info = game.state.secondary_state_info
    if secondary_state[0:6] == 'STATE_' and secondary_state[6:] in GAME_INTERRUPTIONS:
        kick = secondary_state[6:]
        step = secondary_state_info[1]
        delay = (time_count - game.interruption_step_time) / 1000
        if step == 0 and game.interruption_step != step:
            game.interruption_step = step
            game.interruption_step_time = time_count
            info(f'Awarding a {GAME_INTERRUPTIONS[kick]}.')
        elif (step == 1 and game.interruption_step != step and game.state.secondary_seconds_remaining <= 0 and
              delay >= SIMULATED_TIME_INTERRUPTION_PHASE_1):
            game.interruption_step = step
            game_controller_send(f'{kick}:{secondary_state_info[0]}:PREPARE')
            game.interruption_step_time = time_count
            info(f'Prepare for {GAME_INTERRUPTIONS[kick]}.')
        elif step == 2 and game.interruption_step != step and game.state.secondary_seconds_remaining <= 0:
            game.interruption_step = step
            opponent_team = blue_team if secondary_state_info[0] == game.red.id else red_team
            check_team_away_from_ball(opponent_team, game.field.opponent_distance_to_ball)
            game_controller_send(f'{kick}:{secondary_state_info[0]}:EXECUTE')
            info(f'Execute {GAME_INTERRUPTIONS[kick]}.')
            game.interruption_seconds = game.state.seconds_remaining
    elif secondary_state not in ['STATE_NORMAL', 'STATE_OVERTIME', 'STATE_PENALTYSHOOT']:
        print(f'GameController {game.state.game_state}:{secondary_state}: {secondary_state_info}')
    update_penalized()


def game_controller_send(message):
    if message[:6] == 'STATE:' or message[:6] == 'SCORE:' or message == 'DROPPEDBALL':
        # we don't want to send twice the same STATE or SCORE message
        if game_controller_send.sent_once == message:
            return False
        game_controller_send.sent_once = message
        if message[6:] in ['READY', 'SET']:
            game.wait_for_state = message[6:]
        elif message[6:] == 'PLAY':
            game.wait_for_state = 'PLAYING'
        elif (message[:6] == 'SCORE:' or
              message == 'DROPPEDBALL' or
              message[:11] == 'CORNERKICK:' or
              message[:9] == 'GOALKICK:' or
              message[:8] == 'THROWIN:' or
              message[:16] == 'DIRECT_FREEKICK:' or
              message[:18] == 'INDIRECT_FREEKICK:' or
              message[:12] == 'PENALTYKICK:'):
            game.wait_for_state = 'READY'
    game_controller_send.id += 1
    if message[:6] != 'CLOCK:':
        info(f'Sending {game_controller_send.id}:{message} to GameController.')
    message = f'{game_controller_send.id}:{message}\n'
    game.controller.sendall(message.encode('ascii'))
    # info(f'sending {message.strip()} to GameController')
    game_controller_send.unanswered[game_controller_send.id] = message.strip()
    answered = False
    sent_id = game_controller_send.id
    while True:
        try:
            answers = game.controller.recv(1024).decode('ascii').split('\n')
            for answer in answers:
                if answer == '':
                    continue
                try:
                    id, result = answer.split(':')
                    if int(id) == sent_id:
                        answered = True
                except ValueError:
                    error(f'Cannot split {answer}')
                try:
                    answered_message = game_controller_send.unanswered[int(id)]
                    del game_controller_send.unanswered[int(id)]
                except KeyError:
                    error(f'Received acknowledgment message for unknown message: {id}')
                    continue
                if result == 'OK':
                    continue
                if result == 'INVALID':
                    error(f'Received invalid answer from GameController for message {answered_message}.')
                elif result == 'ILLEGAL':
                    error(f'Received illegal answer from GameController for message {answered_message}.')
                else:
                    error(f'Received unknown answer from GameController: {answer}.')
        except BlockingIOError:
            if not game.game_controller_synchronization:
                break
            elif answered or ':CLOCK:' in message:
                break
            else:  # keep sending CLOCK messages to keep the GameController happy
                info(f'Waiting for GameController to answer to {message.strip()}.')
                time.sleep(0.2)
                game_controller_send.id += 1
                clock_message = f'{game_controller_send.id}:CLOCK:{time_count}\n'
                game.controller.sendall(clock_message.encode('ascii'))
                game_controller_send.unanswered[game_controller_send.id] = clock_message.strip()

    return True


game_controller_send.id = 0
game_controller_send.unanswered = {}
game_controller_send.sent_once = None


def distance2(v1, v2):
    return math.sqrt((v1[0] - v2[0]) ** 2 + (v1[1] - v2[1]) ** 2)


def distance3(v1, v2):
    return math.sqrt((v1[0] - v2[0]) ** 2 + (v1[1] - v2[1]) ** 2 + (v1[2] - v2[2]) ** 2)


def rotate_along_z(axis_and_angle):
    q = transforms3d.quaternions.axangle2quat([axis_and_angle[0], axis_and_angle[1], axis_and_angle[2]], axis_and_angle[3])
    rz = [0, 0, 0, 1]
    # rz = [0, 0, 0.7071068, 0.7071068]
    r = transforms3d.quaternions.qmult(q, rz)
    v, a = transforms3d.quaternions.quat2axangle(r)
    return [v[0], v[1], v[2], a]


def append_solid(solid, solids):  # we list only the hands and feet
    model_field = solid.getField('model')
    if model_field:
        model = model_field.getSFString()
        suffix = model[-4:]
        if suffix == 'hand' or suffix == 'foot':
            solids.append(solid)
    children = solid.getProtoField('children') if solid.isProto() else solid.getField('children')
    for i in range(children.getCount()):
        child = children.getMFNode(i)
        if child.getType() in [Node.ROBOT, Node.SOLID, Node.GROUP, Node.TRANSFORM, Node.ACCELEROMETER, Node.CAMERA, Node.GYRO,
                               Node.TOUCH_SENSOR]:
            append_solid(child, solids)
            continue
        if child.getType() in [Node.HINGE_JOINT, Node.HINGE_2_JOINT, Node.SLIDER_JOINT, Node.BALL_JOINT]:
            endPoint = child.getProtoField('endPoint') if child.isProto() else child.getField('endPoint')
            solid = endPoint.getSFNode()
            if solid.getType() == Node.NO_NODE:
                continue
            append_solid(solid, solids)


def list_team_solids(team):
    for number in team['players']:
        player = team['players'][number]
        robot = player['robot']
        player['solids'] = []
        solids = player['solids']
        append_solid(robot, solids)
        if len(solids) != 4:
            error(f"{team['color']} player {number} is missing a hand or a foot.")


def list_solids():
    list_team_solids(red_team)
    list_team_solids(blue_team)


def area(x1, y1, x2, y2, x3, y3):
    return abs((x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2)) / 2.0)


def point_in_triangle(m, a, b, c):
    abc = area(a[0], a[1], b[0], b[1], c[0], c[1])
    mbc = area(m[0], m[1], b[0], b[1], c[0], c[1])
    amc = area(a[0], a[1], m[0], m[1], c[0], c[1])
    amb = area(a[0], a[1], b[0], b[1], m[0], m[1])
    if abc == mbc + amc + amb:
        return True
    else:
        return False


def aabb_circle_collision(aabb, x, y, radius):
    if x + radius < aabb[0]:
        return False
    if x - radius > aabb[2]:
        return False
    if y + radius < aabb[1]:
        return False
    if y - radius > aabb[3]:
        return False
    return True


def segment_circle_collision(p1, p2, center, radius):
    len = distance2(p1, p2)
    dx = (p2[0] - p1[0]) / len
    dy = (p2[1] - p1[1]) / len
    t = dx * (center[0] - p1[0]) + dy * (center[1] - p1[1])
    e = [t * dx + p1[0], t * dy + p1[1]]  # projection of circle center onto the segment
    d = distance2(e, center)
    return d < radius


def triangle_circle_collision(p1, p2, p3, center, radius):
    if distance2(p1, center) < radius or distance2(p2, center) < radius or distance2(p3, center) < radius:
        return True
    if point_in_triangle(center, p1, p2, p3):
        return True
    return segment_circle_collision(p1, p2, center, radius) \
        or segment_circle_collision(p1, p3, center, radius) \
        or segment_circle_collision(p2, p3, center, radius)


def update_team_convex_hulls(team):
    for number in team['players']:
        player = team['players'][number]
        player['shadow'] = []
        if 'aabb' in player:
            del player['aabb']
        shadow = player['shadow']
        for solid in player['solids']:
            position = solid.getPosition()
            shadow.append([position[0], position[1]])
            if 'aabb' not in player:
                player['aabb'] = [position[0], position[1], position[0], position[1]]
            else:
                if position[0] < player['aabb'][0]:
                    player['aabb'][0] = position[0]
                elif position[0] > player['aabb'][2]:
                    player['aabb'][2] = position[0]
                if position[1] < player['aabb'][1]:
                    player['aabb'][1] = position[1]
                elif position[1] > player['aabb'][3]:
                    player['aabb'][3] = position[1]
        # check AABB for ball collision
        if not aabb_circle_collision(player['aabb'], game.ball_position[0], game.ball_position[1], game.ball_radius):
            hold_ball = False
        else:  # compute convex hull of quad
            if point_in_triangle(shadow[0], shadow[1], shadow[2], shadow[3]):
                del shadow[0]
            elif point_in_triangle(shadow[1], shadow[0], shadow[2], shadow[3]):
                del shadow[1]
            elif point_in_triangle(shadow[2], shadow[0], shadow[1], shadow[3]):
                del shadow[2]
            elif point_in_triangle(shadow[3], shadow[0], shadow[1], shadow[2]):
                del shadow[3]
            if len(shadow) == 3:
                hold_ball = triangle_circle_collision(shadow[0], shadow[1], shadow[2], game.ball_position, game.ball_radius)
            else:
                hold_ball = triangle_circle_collision(shadow[0], shadow[1], shadow[2], game.ball_position, game.ball_radius) \
                         or triangle_circle_collision(shadow[3], shadow[1], shadow[2], game.ball_position, game.ball_radius)
        color = team['color']
        if 'hold_ball' in player:
            if hold_ball:
                continue
            delay = int((time_count - player['hold_ball']) / 100) / 10
            info(f'{color.capitalize()} player {number} released the ball after {delay} seconds.')
            del player['hold_ball']
        elif hold_ball:
            player['hold_ball'] = time_count
            info(f'{color.capitalize()} player {number} is holding the ball.')


def update_convex_hulls():
    update_team_convex_hulls(red_team)
    update_team_convex_hulls(blue_team)


def init_team(team):
    for number in team['players']:
        player = team['players'][number]
        player['outside_circle'] = True
        player['outside_field'] = True
        player['inside_field'] = False
        player['inside_own_side'] = False
        player['outside_goal_area'] = True
        player['left_turf_time'] = None


def update_team_contacts(team):
    color = team['color']
    for number in team['players']:
        player = team['players'][number]
        robot = player['robot']
        n = robot.getNumberOfContactPoints(True)
        player['contact_points'] = []
        if n == 0:  # robot is asleep
            continue
        player['position'] = robot.getCenterOfMass()
        player['outside_circle'] = True        # true if fully outside the center cicle
        player['outside_field'] = True         # true if fully outside the field
        player['inside_field'] = True          # true if fully inside the field
        player['inside_own_side'] = True       # true if fully inside its own side (half field side)
        player['outside_goal_area'] = True     # true if fully outside of any goal area
        outside_turf = True                    # true if fully outside turf
        fallen = False
        for i in range(n):
            point = robot.getContactPoint(i)
            if point[2] > game.field.turf_depth:  # not a contact with the ground
                if point in game.ball.contact_points:  # ball contact
                    if game.ball_first_touch_time == 0:
                        game.ball_first_touch_time = time_count
                    game.ball_last_touch_time = time_count
                    if game.penalty_shootout_count >= 10:  # extended penalty shootout
                        game.penalty_shootout_time_to_touch_ball[game.penalty_shootout_count - 10] = \
                          60 - game.state.seconds_remaining
                    if game.ball_last_touch_team != color or game.ball_last_touch_player_number != int(number):
                        game.ball_last_touch_team = color
                        game.ball_last_touch_player_number = int(number)
                        game.ball_last_touch_time_for_display = time_count
                        action = 'kicked' if game.kicking_player_number is None else 'touched'
                        info(f'Ball {action} by {color} player {number}.')
                        if game.kicking_player_number is None:
                            game.kicking_player_number = int(number)
                    elif time_count - game.ball_last_touch_time_for_display >= 1000:  # dont produce too many touched messages
                        game.ball_last_touch_time_for_display = time_count
                        info(f'Ball touched again by {color} player {number}.')
                    continue
                # the robot touched something else than the ball or the ground
                player['contact_points'].append(point)  # this list will be checked later for robot-robot collisions
                continue
            if distance2(point, [0, 0]) < game.field.circle_radius:
                player['outside_circle'] = False
            if game.field.point_inside(point, include_turf=True):
                outside_turf = False
            if game.field.point_inside(point):
                player['outside_field'] = False
                if abs(point[0]) > game.field.size_x - game.field.goal_area_length and \
                   abs(point[1]) < game.field.goal_area_width / 2:
                    player['outside_goal_area'] = False
            else:
                player['inside_field'] = False
            if game.side_left == (game.red.id if color == 'red' else game.blue.id):
                if point[0] > -LINE_HALF_WIDTH:
                    player['inside_own_side'] = False
            else:
                if point[0] < LINE_HALF_WIDTH:
                    player['inside_own_side'] = False
            # check if the robot has fallen
            node = robot.getContactPointNode(i)
            if not node:
                continue
            model_field = node.getField('model')
            if model_field is None:
                continue
            model = model_field.getSFString()
            if model[-4:] == 'foot':
                continue
            if 'fallen' in player:  # was already down
                fallen = True
                continue
            info(f'{color} player {number} has fallen down.')
            player['fallen'] = time_count
        if not fallen and 'fallen' in player:  # the robot has recovered
            delay = (int((time_count - player['fallen']) / 100)) / 10
            info(f'{color} player {number} just recovered after {delay} seconds.')
            del player['fallen']
        if outside_turf:
            if player['left_turf_time'] is None:
                player['left_turf_time'] = time_count
        else:
            player['left_turf_time'] = None


def update_ball_contacts():
    game.ball.contact_points = []
    for i in range(game.ball.getNumberOfContactPoints()):
        point = game.ball.getContactPoint(i)
        if point[2] <= 0.01:  # contact with the ground
            continue
        game.ball.contact_points.append(point)
        break


def update_contacts():
    update_ball_contacts()
    update_team_contacts(red_team)
    update_team_contacts(blue_team)


def update_team_penalized(team):
    for number in team['players']:
        n = game.state.teams[team_index(team['color'])].players[int(number) - 1].secs_till_unpenalized
        player = team['players'][number]
        if n > 0:
            player['penalized'] = n
            if 'sent_to_penality_position' in player:
                del player['sent_to_penality_position']
        elif 'penalized' in player:
            del player['penalized']


def update_penalized():
    update_team_penalized(red_team)
    update_team_penalized(blue_team)


def send_penalty(player, penalty, reason, log=None):
    if 'sent_to_penality_position' in player:  # was just penalized
        return
    player['penalty'] = penalty
    player['penalty_reason'] = reason
    if log is not None:
        info(log)


def check_team_ball_holding(team):
    for number in team['players']:
        player = team['players'][number]
        if 'hold_ball' in player:
            color = team['color']
            dt = (time_count - player['hold_ball']) / 1000
            if number == '1':
                if dt > GOAL_KEEPER_BALL_HOLDING_TIMEOUT:
                    del player['hold_ball']
                    info(f'{color.capitalize()} player {number} (goal keeper) has held the ball for too long.')
                    return True
            elif dt > PLAYER_BALL_HOLDING_TIMEOUT:
                del player['hold_ball']
                info(f'{color.capitalize()} player {number} has held the ball for too long.')
                return True
    return False


def check_ball_holding():  # return the team id which gets a free kick in case of ball holding from the other team
    if check_team_ball_holding(red_team):
        return game.blue.id
    if check_team_ball_holding(blue_team):
        return game.red.id
    return None


def check_team_fallen(team):
    color = team['color']
    penalty = False
    for number in team['players']:
        player = team['players'][number]
        if 'penalized' in player:
            continue  # skip penalized players
        if 'fallen' in player and time_count - player['fallen'] > 1000 * FALLEN_TIMEOUT:
            del player['fallen']
            send_penalty(player, 'INCAPABLE', 'fallen down',
                         f'{color} player {number} has fallen down and didn\'t recover in the last 20 seconds.')
            penalty = True
    return penalty


def check_fallen():
    red = check_team_fallen(red_team)
    blue = check_team_fallen(blue_team)
    return red or blue


def check_team_inactive_goalie(team):
    for number in team['players']:
        if not is_goal_keeper(team, number):
            continue
        player = team['players'][number]
        if 'penalized' in player:
            continue  # skip penalized players
        if 'previous_position' not in player:
            player['previous_position'] = player['position']
            return
        move = distance2(player['previous_position'], game.ball_position) - distance2(player['position'], game.ball_position)
        if move > 0:  # moving toward the ball
            info('Goal keeper moving towards the ball.')


def check_inactive_goalie():
    check_team_inactive_goalie(red_team)
    check_team_inactive_goalie(blue_team)


def check_team_away_from_ball(team, distance):
    for number in team['players']:
        player = team['players'][number]
        if 'penalized' in player:
            continue  # skip penalized players
        d = distance2(player['position'], game.ball_position)
        if d < distance:
            color = team['color']
            send_penalty(player, 'INCAPABLE', f'too close to ball: {d:.2f} m., should be at least {distance:.2f} m.' +
                         f'{color.capitalize()} player {number} is too close to the ball: ' +
                         f'({d:.2f} m., should be at least {distance:.2f} m.)')


def check_team_start_position(team):
    penalty = False
    for number in team['players']:
        player = team['players'][number]
        if 'penalized' in player:
            continue
        if not player['outside_field']:
            send_penalty(player, 'INCAPABLE', 'halfTimeStartingPose inside field')
            penalty = True
        elif not player['inside_own_side']:
            send_penalty(player, 'INCAPABLE', 'halfTimeStartingPose outside team side')
            penalty = True
    return penalty


def check_start_position():
    red = check_team_start_position(red_team)
    blue = check_team_start_position(blue_team)
    return red or blue


def check_team_kickoff_position(team):
    color = team['color']
    team_id = game.red.id if color == 'red' else game.blue.id
    penalty = False
    for number in team['players']:
        player = team['players'][number]
        if 'penalized' in player:
            continue  # skip penalized players
        if not player['inside_field']:
            send_penalty(player, 'INCAPABLE', 'outside the field at kick-off')
            penalty = True
        elif not player['inside_own_side']:
            send_penalty(player, 'INCAPABLE', 'outside team side at kick-off')
            penalty = True
        elif game.kickoff != team_id and not player['outside_circle']:
            send_penalty(player, 'INCAPABLE', 'inside center circle during oppenent\'s kick-off')
            penalty = True
    return penalty


def check_kickoff_position():
    red = check_team_kickoff_position(red_team)
    blue = check_team_kickoff_position(blue_team)
    return red or blue


def check_team_dropped_ball_position(team):
    for number in team['players']:
        player = team['players'][number]
        if 'penalized' in player:
            continue
        if not player['outside_circle']:
            send_penalty(player, 'INCAPABLE', 'inside center circle during dropped ball')
        elif not player['inside_own_side']:
            send_penalty(player, 'INCAPABLE', 'outside team side during dropped ball')


def check_dropped_ball_position():
    check_team_dropped_ball_position(red_team)
    check_team_dropped_ball_position(blue_team)


def check_team_outside_turf(team):
    color = team['color']
    for number in team['players']:
        player = team['players'][number]
        if 'penalized' in player:
            continue  # skip already penalized players
        if player['left_turf_time'] is None:
            continue
        if time_count - player['left_turf_time'] < OUTSIDE_TURF_TIMEOUT * 1000:
            continue
        send_penalty(player, 'INCAPABLE', f'left the field for more than {OUTSIDE_TURF_TIMEOUT} seconds',
                     f'{color.capitalize()} player {number} left the field for more than {OUTSIDE_TURF_TIMEOUT} seconds.')


def check_outside_turf():
    check_team_outside_turf(red_team)
    check_team_outside_turf(blue_team)


def check_team_penalized_in_field(team):
    color = team['color']
    penalty = False
    for number in team['players']:
        player = team['players'][number]
        if 'penalized' not in player:
            continue  # skip non penalized players
        if player['outside_field']:
            continue
        game_controller_send(f'CARD:{game.red.id if color == "red" else game.blue.id}:{number}:YELLOW')
        send_penalty(player, 'INCAPABLE', 'penalized player re-entered field',
                     f'Penalized {color} player {number} re-entered the field: shown yellow card.')
        penalty = True
    return penalty


def check_penalized_in_field():
    red = check_team_penalized_in_field(red_team)
    blue = check_team_penalized_in_field(blue_team)
    return red or blue


def check_circle_entrance(team):
    penalty = False
    for number in team['players']:
        player = team['players'][number]
        if 'penalized' in player:
            continue
        if not player['outside_circle']:
            color = team['color']
            send_penalty(player, 'INCAPABLE', 'entered circle during oppenent\'s kick-off',
                         f'{color.capitalize()} player {number} entering circle during opponent\'s kick-off.')
            penalty = True
    return penalty


def check_ball_must_kick(team):
    if game.ball_last_touch_team == game.ball_must_kick_team:
        return False  # no foul
    info(f'Ball was touched by wrong team.')
    for number in team['players']:
        if not game.ball_last_touch_player_number == int(number):
            continue
        player = team['players'][number]
        if 'penalized' in player:
            continue
        color = team['color']
        send_penalty(player, 'INCAPABLE', 'non-kicking player touched ball not in play',
                     f'Non-kicking {color} player {number} touched ball not in play.')
        break
    return True


def send_team_penalties(team):
    color = team['color']
    for number in team['players']:
        player = team['players'][number]
        if 'penalty' in player:
            penalty = player['penalty']
            reason = player['penalty_reason']
            del player['penalty']
            del player['penalty_reason']
            team_id = game.red.id if color == 'red' else game.blue.id
            game_controller_send(f'PENALTY:{team_id}:{number}:{penalty}')
            robot = player['robot']
            robot.resetPhysics()
            translation = robot.getField('translation')
            rotation = robot.getField('rotation')
            t = copy.deepcopy(team['players'][number]['reentryStartingPose']['translation'])
            r = copy.deepcopy(team['players'][number]['reentryStartingPose']['rotation'])
            t[0] = game.field.penalty_mark_x if t[0] > 0 else -game.field.penalty_mark_x
            if (game.ball_position[1] > 0 and t[1] > 0) or (game.ball_position[1] < 0 and t[1] < 0):
                t[1] = -t[1]
                r = rotate_along_z(r)
            # check if position is already occupied by a penalized robot
            while True:
                moved = False
                for n in team['players']:
                    other_robot = team['players'][n]['robot']
                    other_t = other_robot.getField('translation').getSFVec3f()
                    if distance3(other_t, t) < game.field.robot_radius:
                        t[0] += game.field.penalty_offset if game.ball_position[0] < t[0] else -game.field.penalty_offset
                        moved = True
                if not moved:
                    break
            # test if position is behind the goal line (note: it should never end up beyond the center line)
            if t[0] > game.field.size_x:
                t[0] -= 4 * game.field.penalty_offset
            elif t[0] < -game.field.size_x:
                t[0] += 4 * game.field.penalty_offset
            translation.setSFVec3f(t)
            rotation.setSFRotation(r)
            player['sent_to_penality_position'] = True
            info(f'{penalty} penalty for {color} player {number}: {reason}. Sent to ' +
                 f'translation ({t[0]} {t[1]} {t[2]}), rotation ({r[0]} {r[1]} {r[2]} {r[3]}).')


def send_penalties():
    send_team_penalties(red_team)
    send_team_penalties(blue_team)


def flip_pose(pose):
    pose['translation'][0] = -pose['translation'][0]
    pose['rotation'][3] = math.pi - pose['rotation'][3]


def flip_poses(player):
    flip_pose(player['halfTimeStartingPose'])
    flip_pose(player['reentryStartingPose'])
    flip_pose(player['shootoutStartingPose'])
    flip_pose(player['goalKeeperStartingPose'])


def flip_sides():  # flip sides (no need to notify GameController, it does it automatically)
    game.side_left = game.red.id if game.side_left == game.blue.id else game.blue.id
    for team in [red_team, blue_team]:
        for number in team['players']:
            flip_poses(team['players'][number])


def reset_player(color, number, pose):
    team = red_team if color == 'red' else blue_team
    player = team['players'][number]
    player['robot'].resetPhysics()
    translation = player['robot'].getField('translation')
    rotation = player['robot'].getField('rotation')
    t = player[pose]['translation']
    r = player[pose]['rotation']
    translation.setSFVec3f(t)
    rotation.setSFRotation(r)
    info(f'{color} player {number} reset to {pose}: ' +
         f'translation ({t[0]} {t[1]} {t[2]}), rotation ({r[0]} {r[1]} {r[2]} {r[3]}).')


def reset_teams(pose):
    for number in red_team['players']:
        reset_player('red', str(number), pose)
    for number in blue_team['players']:
        reset_player('blue', str(number), pose)


def is_goal_keeper(team, id):
    return id == '1'  # assuming goal keeper is player number 1


def is_penalty_kicker(team, id):
    return id == '1'  # assuming kicker is player number 1


def penalty_kicker_player():
    default = game.penalty_shootout_count % 2 == 0
    attacking_team = red_team if game.kickoff == game.red.id and default else blue_team
    for number in attacking_team['players']:
        if is_penalty_kicker(attacking_team, number):
            return attacking_team['players'][number]


def set_penalty_positions():
    default = game.penalty_shootout_count % 2 == 0
    attacking_color = 'red' if game.kickoff == game.red.id and default else 'blue'
    if attacking_color == 'red':
        defending_color = 'blue'
        attacking_team = red_team
        defending_team = blue_team
    else:
        defending_color = 'red'
        attacking_team = blue_team
        defending_team = red_team
    for number in attacking_team['players']:
        if is_penalty_kicker(attacking_team, number):
            reset_player(attacking_color, number, 'shootoutStartingPose')
        else:
            reset_player(attacking_color, number, 'halfTimeStartingPose')
    for number in defending_team['players']:
        if is_goal_keeper(defending_team, number) and game.penalty_shootout_count < 10:
            reset_player(defending_color, number, 'goalKeeperStartingPose')
        else:
            reset_player(defending_color, number, 'halfTimeStartingPose')
    x = game.field.penalty_mark_x if game.side_left == game.kickoff and default else -game.field.penalty_mark_x
    game.ball.resetPhysics()
    game.ball_kick_translation[0] = x
    game.ball_kick_translation[1] = 0
    game.ball_translation.setSFVec3f(game.ball_kick_translation)


def stop_penalty_shootout():
    if game.penalty_shootout_count > 10:
        message = 'End of penalty shoot-out.'
    else:
        message = 'End of extended penalty shoot-out.'
    if game.penalty_shootout_count == 20:  # end of extended penalty shootout
        info(message)
        return True
    diff = abs(game.state.teams[0].score - game.state.teams[1].score)
    if game.penalty_shootout_count == 10 and diff > 0:
        info(message)
        return True
    kickoff_team = game.state.teams[0] if game.kickoff == game.state.teams[0].team_number else game.state.teams[1]
    kickoff_team_leads = kickoff_team.score >= game.state.teams[0].score and kickoff_team.score >= game.state.teams[1].score
    penalty_shootout_count = game.penalty_shootout_count % 10  # supports both regular and extended shootout kicks
    if (penalty_shootout_count == 6 and diff == 3) or (penalty_shootout_count == 8 and diff == 2):
        info(message)
        return True  # no need to go further, score is like 3-0 after 6 shootouts or 4-2 after 8 shootouts
    if penalty_shootout_count == 7:
        if diff == 3:  # score is like 4-1
            info(message)
            return True
        if diff == 2 and not kickoff_team_leads:  # score is like 1-3
            info(message)
            return True
    elif penalty_shootout_count == 9:
        if diff == 2:  # score is like 5-3
            info(message)
            return True
        if diff == 1 and not kickoff_team_leads:  # score is like 3-4
            info(message)
            return True
    return False


def next_penalty_shootout():
    game.penalty_shootout_count += 1
    if not game.penalty_shootout_goal:
        game_controller_send('STATE:FINISH')
    if stop_penalty_shootout():
        game.over = True
        return
    if game.penalty_shootout_count == 10:
        info('Starting extended penalty shootout without a goal keeper and goal area entrance allowed.')
    flip_sides()
    info(f'fliped sides: game.side_left = {game.side_left}')
    set_penalty_positions()
    game_controller_send('STATE:SET')
    game.play_countdown = SIMULATED_TIME_BEFORE_PLAY_STATE
    return


def interruption(type, team=None):
    game.in_play = None
    game.can_score_own = False
    game.ball_set_kick = True
    game.interruption = type
    game.phase = type
    game.ball_first_touch_time = 0
    game.interruption_countdown = SIMULATED_TIME_INTERRUPTION_PHASE_0
    if not team:
        game.interruption_team = game.red.id if game.ball_last_touch_team == 'blue' else game.blue.id
    else:
        game.interruption_team = team
    game.ball_must_kick_team = 'red' if game.interruption_team == game.red.id else 'blue'
    game.ball_last_touch_team = game.ball_must_kick_team
    info(f'Ball not in play, will be kicked by a player from the {game.ball_must_kick_team} team.')
    color = 'red' if game.interruption_team == game.red.id else 'blue'
    info(f'{GAME_INTERRUPTIONS[type].capitalize()} awarded to {color} team.')
    game_controller_send(f'{game.interruption}:{game.interruption_team}')


def throw_in(left_side):
    # set the ball on the touch line for throw in
    sign = -1 if left_side else 1
    game.ball_kick_translation[0] = game.ball_exit_translation[0]
    game.ball_kick_translation[1] = sign * (game.field.size_y - LINE_HALF_WIDTH)
    game.can_score = False  # disallow direct goal
    interruption('THROWIN')


def corner_kick(left_side):
    # set the ball in the right corner for corner kick
    sign = -1 if left_side else 1
    game.ball_kick_translation[0] = sign * (game.field.size_x - LINE_HALF_WIDTH)
    game.ball_kick_translation[1] = game.field.size_y - LINE_HALF_WIDTH if game.ball_exit_translation[1] > 0 \
        else -game.field.size_y + LINE_HALF_WIDTH
    game.can_score = True
    interruption('CORNERKICK')


def goal_kick():
    # set the ball at intersection between the centerline and touchline
    game.ball_kick_translation[0] = 0
    game.ball_kick_translation[1] = game.field.size_y - LINE_HALF_WIDTH if game.ball_exit_translation[1] > 0 \
        else -game.field.size_y + LINE_HALF_WIDTH
    game.can_score = True
    interruption('GOALKICK')


def kickoff():
    color = 'red' if game.kickoff == game.red.id else 'blue'
    info(f'Kick-off is {color}.')
    game.phase = 'KICKOFF'
    game.ball_kick_translation[0] = 0
    game.ball_kick_translation[1] = 0
    game.ball_set_kick = True
    game.ball_first_touch_time = 0
    game.in_play = None
    game.ball_must_kick_team = color
    game.ball_last_touch_team = game.ball_must_kick_team
    game.ball_last_touch_player_number = 0
    game.ball_left_circle = None  # one can score only after ball went out of the circle
    game.can_score = False        # or was touched by another player
    game.can_score_own = False
    game.kicking_player_number = None
    info(f'Ball not in play, will be kicked by a player from the {game.ball_must_kick_team} team.')


def dropped_ball():
    info(f'The ball didn\'t move for the past {DROPPED_BALL_TIMEOUT} seconds.')
    game.ball_last_move = time_count
    game_controller_send('DROPPEDBALL')
    game.phase = 'DROPPEDBALL'
    game.ball_kick_translation[0] = 0
    game.ball_kick_translation[1] = 0
    game.ball_set_kick = True
    game.ball_first_touch_time = 0
    game.in_play = None
    game.dropped_ball = True
    game.can_score = True
    game.can_score_own = False


time_count = 0

log_file = open('log.txt', 'w')

# determine configuration file name
game_config_file = os.environ['WEBOTS_ROBOCUP_GAME'] if 'WEBOTS_ROBOCUP_GAME' in os.environ \
    else os.path.join(os.getcwd(), 'game.json')
if not os.path.isfile(game_config_file):
    error(f'Cannot read {game_config_file} game config file.')

# read configuration files
with open(game_config_file) as json_file:
    game = json.loads(json_file.read(), object_hook=lambda d: SimpleNamespace(**d))
with open(game.red.config) as json_file:
    red_team = json.load(json_file)
with open(game.blue.config) as json_file:
    blue_team = json.load(json_file)

# finalize the game object
if not hasattr(game, 'minimum_real_time_factor'):
    game.minimum_real_time_factor = 3  # we garantee that each time step lasts at least 3x simulated time
if not hasattr(game, 'press_a_key_to_terminate'):
    game.press_a_key_to_terminate = False
if not hasattr(game, 'game_controller_synchronization'):
    game.game_controller_synchronization = False
if game.type not in ['NORMAL', 'KNOCKOUT', 'PENALTY']:
    error(f'Unsupported game type: {game.type}.')
game.penalty_shootout = game.type == 'PENALTY'
info(f'Minimum real time factor is set to {game.minimum_real_time_factor}.')
if game.minimum_real_time_factor == 0:
    info('Simulation will run as fast as possible, real time waiting times will be minimal.')
else:
    info(f'Simulation will guarantee a maximum {1 / game.minimum_real_time_factor:.2f}x speed for each time step.')
field_size = getattr(game, 'class').lower()
game.field = Field(field_size)

red_team['color'] = 'red'
blue_team['color'] = 'blue'
init_team(red_team)
init_team(blue_team)

# check team name length (should be at most 12 characters long, trim them if too long)
if len(red_team['name']) > 12:
    red_team['name'] = red_team['name'][:12]
if len(blue_team['name']) > 12:
    blue_team['name'] = blue_team['name'][:12]

# check if the host parameter of the game.json file correspond to the actual host
host = socket.gethostbyname(socket.gethostname())
if host != '127.0.0.1' and host != game.host:
    error(f'Host is not correctly defined in game.json file, it should be {host} instead of {game.host}.')

# launch the GameController
try:
    JAVA_HOME = os.environ['JAVA_HOME']
    try:
        GAME_CONTROLLER_HOME = os.environ['GAME_CONTROLLER_HOME']
        if not os.path.exists(GAME_CONTROLLER_HOME):
            error(f'{GAME_CONTROLLER_HOME} (GAME_CONTROLLER_HOME) folder not found.')
            game.controller_process = None
        else:
            path = os.path.join(GAME_CONTROLLER_HOME, 'build', 'jar', 'config', f'hl_sim_{field_size}', 'teams.cfg')
            red_line = f'{game.red.id}={red_team["name"]}\n'
            blue_line = f'{game.blue.id}={blue_team["name"]}\n'
            with open(path, 'w') as file:
                file.write((red_line + blue_line) if game.red.id < game.blue.id else (blue_line + red_line))
            command_line = [os.path.join(JAVA_HOME, 'bin', 'java'), '-jar', 'GameControllerSimulator.jar']
            if game.minimum_real_time_factor < 1:
                command_line.append('--fast')
            command_line.append('--config')
            command_line.append(game_config_file)
            game.controller_process = subprocess.Popen(command_line, cwd=os.path.join(GAME_CONTROLLER_HOME, 'build', 'jar'))
    except KeyError:
        GAME_CONTROLLER_HOME = None
        game.controller_process = None
        error('GAME_CONTROLLER_HOME environment variable not set, unable to launch GameController.')
except KeyError:
    JAVA_HOME = None
    GAME_CONTROLLER_HOME = None
    game.controller_process = None
    error('JAVA_HOME environment variable not set, unable to launch GameController.')

toss_a_coin_if_needed('side_left')
toss_a_coin_if_needed('kickoff')

# start the webots supervisor
supervisor = Supervisor()
root = supervisor.getRoot()
children = root.getField('children')

children.importMFNodeFromString(-1, f'RobocupSoccerField {{ size "{field_size}" }}')
ball_size = 1 if field_size == 'kid' else 5
# the ball is initially very far away from the field
children.importMFNodeFromString(-1, f'DEF BALL RobocupSoccerBall {{ translation 100 100 0.5 size {ball_size} }}')

game.state = None
game.font_size = 0.1
game.font = 'Lucida Console'
game.overlay_x = 0.02
game.overlay_y = 0.01
spawn_team(red_team, game.side_left == game.blue.id, children)
spawn_team(blue_team, game.side_left == game.red.id, children)
setup_display()

time_step = int(supervisor.getBasicTimeStep())
SIMULATED_TIME_INTERRUPTION_PHASE_0 = int(SIMULATED_TIME_INTERRUPTION_PHASE_0 * 1000 / time_step)
SIMULATED_TIME_BEFORE_PLAY_STATE = int(SIMULATED_TIME_BEFORE_PLAY_STATE * 1000 / time_step)

if game.controller_process:
    game.controller = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    retry = 0
    while True:
        try:
            game.controller.connect(('localhost', 8750))
            game.controller.setblocking(False)
            break
        except socket.error as msg:
            retry += 1
            if retry <= 10:
                warning(f'Could not connect to GameController at localhost:8750: {msg}. Retrying ({retry}/10)...')
                time.sleep(retry)  # give some time to allow the GameControllerSimulator to start-up
                supervisor.step(time_step)
            else:
                error('Could not connect to GameController at localhost:8750.')
                game.controller = None
                break
    info('Connected to GameControllerSimulator at localhost:8750.')
    game.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    game.udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    game.udp.bind(('0.0.0.0', 3838))
    game.udp.setblocking(False)
else:
    game.controller = None

update_state_display()

list_solids()  # prepare lists of solids to monitor in each robot to compute the convex hulls

info(f'Game type is {game.type}.')
info(f'Red team is "{red_team["name"]}", playing on {"left" if game.side_left == game.red.id else "right"} side.')
info(f'Blue team is "{blue_team["name"]}", playing on {"left" if game.side_left == game.blue.id else "right"} side.')
game_controller_send(f'SIDE_LEFT:{game.side_left}')
game.penalty_shootout_count = 0
game.penalty_shootout_goal = False
game.penalty_shootout_time_to_score = [None, None, None, None, None, None, None, None, None, None]
game.penalty_shootout_time_to_reach_goal_area = [None, None, None, None, None, None, None, None, None, None]
game.penalty_shootout_time_to_touch_ball = [None, None, None, None, None, None, None, None, None, None]
game.ball = supervisor.getFromDef('BALL')
game.ball_radius = 0.07 if field_size == 'kid' else 0.1125
game.ball_kick_translation = [0, 0, game.ball_radius + game.field.turf_depth]  # initial position of a ball before kick
game.ball_translation = supervisor.getFromDef('BALL').getField('translation')
game.ball_exit_translation = None
game.ball_last_touch_team = 'red'
game.ball_last_touch_player_number = 1
game.ball_last_touch_time = 0
game.ball_first_touch_time = 0
game.ball_last_touch_time_for_display = 0
game.ball_position = [0, 0, 0]
game.ball_last_move = 0
game.real_time_multiplier = 1000 / (game.minimum_real_time_factor * time_step) if game.minimum_real_time_factor > 0 else 10
game.interruption = None
game.interruption_countdown = 0
game.interruption_step = None
game.interruption_step_time = 0
game.interruption_team = None
game.interruption_seconds = None
game.dropped_ball = False
game.overtime = False
game.ready_countdown = (int)(REAL_TIME_BEFORE_FIRST_READY_STATE * game.real_time_multiplier)
game.play_countdown = 0
game.sent_finish = False
game.over = False
game.wait_for_state = 'INITIAL'

previous_seconds_remaining = 0
if hasattr(game, 'supervisor'):  # optional supervisor used for CI tests
    children.importMFNodeFromString(-1, f'DEF TEST_SUPERVISOR Robot {{ supervisor TRUE controller "{game.supervisor}" }}')

if game.penalty_shootout:
    info(f'{"Red" if game.kickoff == game.red.id else "Blue"} team will start the penalty shoot-out.')
    game.phase = 'PENALTY-SHOOTOUT'
    # game_controller_send(f'KICKOFF:{game.kickoff}')  # FIXME: GameController says this is illegal => we should fix it.
    # meanwhile, assuming kickoff for red team
else:
    kickoff()
    game_controller_send(f'KICKOFF:{game.kickoff}')

if hasattr(game, 'record_simulation'):
    supervisor.animationStartRecording(game.record_simulation + '.html')

previous_real_time = time.time()
while supervisor.step(time_step) != -1:
    game_controller_send(f'CLOCK:{time_count}')
    game_controller_receive()
    if game.state is None:
        time_count += time_step
        continue
    send_play_state_after_penalties = False
    previous_position = copy.deepcopy(game.ball_position)
    game.ball_position = game.ball_translation.getSFVec3f()
    if game.ball_position != previous_position:
        game.ball_last_move = time_count
    update_contacts()  # check for collisions with the ground and ball
    update_convex_hulls()  # badly affects the performance (drop from 5x to 0.5x)
    if game.wait_for_state is not None:  # we are waiting for a new state from the GameController
        time_count += time_step
        continue
    if game.state.game_state == 'STATE_PLAYING':
        check_outside_turf()
        if game.in_play is None:
            if game.phase == 'CORNERKICK' and game.interruption_step != 1 and game.state.secondary_seconds_remaining > 0:
                opponent_team = red_team if game.ball_must_kick_team == 'blue' else blue_team
                check_team_away_from_ball(opponent_team, game.field.opponent_distance_to_ball)
            if game.ball_first_touch_time != 0:
                d = distance2(game.ball_kick_translation, game.ball_position)
                if d > BALL_IN_PLAY_MOVE:
                    info(f'{game.ball_kick_translation} {game.ball_position}')
                    info(f'Ball in play, can be touched by any player (moved by {d * 100:.2f} cm).')
                    game.in_play = time_count
                else:
                    team = red_team if game.ball_must_kick_team == 'blue' else blue_team
                    if not check_circle_entrance(team):
                        check_ball_must_kick(team)
        else:
            if time_count - game.ball_last_move > DROPPED_BALL_TIMEOUT * 1000:
                dropped_ball()
            if game.ball_left_circle is None and game.phase == 'KICKOFF':
                if distance2(game.ball_kick_translation, game.ball_position) > game.field.circle_radius + game.ball_radius:
                    game.ball_left_circle = time_count
                    info('The ball has left the center circle after kick-off.')

            ball_touched_by_opponent = game.ball_last_touch_team != game.ball_must_kick_team
            ball_touched_by_teammate = (game.kicking_player_number is not None
                                        and game.ball_last_touch_player_number != game.kicking_player_number)
            ball_touched_in_play = game.in_play is not None and game.in_play < game.ball_last_touch_time
            if not game.can_score:
                if game.phase == 'KICKOFF':
                    ball_touched_after_leaving_the_circle = game.ball_left_circle is not None \
                                                            and game.ball_left_circle < game.ball_last_touch_time
                    if ball_touched_by_opponent or ball_touched_by_teammate or ball_touched_after_leaving_the_circle:
                        game.can_score = True
                elif game.phase == 'THROWIN':
                    if ball_touched_by_teammate or ball_touched_by_opponent or ball_touched_in_play:
                        game.can_score = True
            if not game.can_score_own:
                if ball_touched_by_opponent or ball_touched_by_teammate or ball_touched_in_play:
                    game.can_score_own = True

        if game.penalty_shootout:
            if game.penalty_shootout_count < 10:  # detect entrance of kicker in the goal area
                kicker = penalty_kicker_player()
                if not kicker['outside_goal_area'] and not kicker['inside_own_side']:
                    # if the kicker is not fully outside the opponent goal area, we stop the kick and continue
                    next_penalty_shootout()
                    if game.over:
                        break
            else:  # extended penalty shootouts
                if game.field.circle_fully_inside_goal_area(game.ball_position, game.ball_radius):
                    c = game.penalty_shootout_count - 10
                    if game.penalty_shootout_time_to_reach_goal_area[c] is None:
                        game.penalty_shootout_time_to_reach_goal_area[c] = 60 - game.state.seconds_remaining
        if previous_seconds_remaining != game.state.seconds_remaining:
            update_state_display()
            previous_seconds_remaining = game.state.seconds_remaining
            if not game.sent_finish and game.state.seconds_remaining <= 0:
                game_controller_send('STATE:FINISH')
                game.sent_finish = True
                if game.penalty_shootout:  # penalty timeout was reached
                    next_penalty_shootout()
                    if game.over:
                        break
                elif game.state.first_half:
                    type = 'knockout ' if game.type == 'KNOCKOUT' and game.overtime else ''
                    info(f'End of {type}first half.')
                    flip_sides()
                    reset_teams('halfTimeStartingPose')
                    game.kickoff = game.blue.id if game.kickoff == game.red.id else game.red.id
                    update_team_display()
                elif game.type == 'NORMAL':
                    info('End of second half.')
                elif game.type == 'KNOCKOUT':
                    if not game.overtime:
                        info('End of second half.')
                        flip_sides()
                        reset_teams('halfTimeStartingPose')
                        game.kickoff = game.blue.id if game.kickoff == game.red.id else game.red.id
                        game.overtime = True
                    else:
                        info('End of knockout second half.')
                else:
                    error(f'Unsupported game type: {game.type}.')
        if game.interruption_countdown == 0 and game.ready_countdown == 0 and \
            (game.ball_position[1] - game.ball_radius >= game.field.size_y or
             game.ball_position[1] + game.ball_radius <= -game.field.size_y or
             game.ball_position[0] - game.ball_radius >= game.field.size_x or
             game.ball_position[0] + game.ball_radius <= -game.field.size_x):
            info(f'Ball left the field at ({game.ball_position[0]} {game.ball_position[1]} {game.ball_position[2]}) after '
                 f'being touched by {game.ball_last_touch_team} player {game.ball_last_touch_player_number}.')
            game.ball_exit_translation = game.ball_position
            scoring_team = None
            right_way = None
            if game.ball_exit_translation[1] - game.ball_radius > game.field.size_y:
                if game.penalty_shootout:
                    game.interruption_countdown = SIMULATED_TIME_INTERRUPTION_PHASE_0
                    game.penalty_shootout_goal = False
                else:
                    game.ball_exit_translation[1] = game.field.size_y - LINE_HALF_WIDTH
                    throw_in(left_side=False)
            elif game.ball_exit_translation[1] + game.ball_radius < -game.field.size_y:
                if game.penalty_shootout:
                    game.interruption_countdown = SIMULATED_TIME_INTERRUPTION_PHASE_0
                    game.penalty_shootout_goal = False
                else:
                    throw_in(left_side=True)
            if game.ball_exit_translation[0] - game.ball_radius > game.field.size_x:
                right_way = game.ball_last_touch_team == 'red' and game.side_left == game.red.id or \
                    game.ball_last_touch_team == 'blue' and game.side_left == game.blue.id
                if game.ball_exit_translation[1] < GOAL_HALF_WIDTH and \
                   game.ball_exit_translation[1] > -GOAL_HALF_WIDTH and game.ball_exit_translation[2] < game.field.goal_height:
                    scoring_team = game.side_left  # goal
                elif game.penalty_shootout:
                    game.interruption_countdown = SIMULATED_TIME_INTERRUPTION_PHASE_0
                    game.penalty_shootout_goal = False
                else:
                    if right_way:
                        goal_kick()
                    else:
                        corner_kick(left_side=False)
            elif game.ball_exit_translation[0] + game.ball_radius < -game.field.size_x:
                right_way = game.ball_last_touch_team == 'red' and game.side_left == game.blue.id or \
                    game.ball_last_touch_team == 'blue' and game.side_left == game.red.id
                if game.ball_exit_translation[1] < GOAL_HALF_WIDTH and \
                   game.ball_exit_translation[1] > -GOAL_HALF_WIDTH and game.ball_exit_translation[2] < game.field.goal_height:
                    # goal
                    scoring_team = game.red.id if game.blue.id == game.side_left else game.blue.id
                elif game.penalty_shootout:
                    game.interruption_countdown = SIMULATED_TIME_INTERRUPTION_PHASE_0
                    game.penalty_shootout_goal = False
                else:
                    if right_way:
                        goal_kick()
                    else:
                        corner_kick(left_side=True)
            if scoring_team:
                goal = 'red' if scoring_team == game.blue.id else 'blue'
                if game.penalty_shootout_count >= 10:  # extended penalty shootouts
                    game.penalty_shootout_time_to_score[game.penalty_shootout_count - 10] = 60 - game.state.seconds_remaining
                if not game.penalty_shootout:
                    game.kickoff = game.blue.id if scoring_team == game.red.id else game.red.id
                i = team_index(game.ball_last_touch_team)
                if not game.can_score:
                    if game.phase == 'KICKOFF':
                        info(f'Invalidated direct score in {goal} goal from kick-off position.')
                        goal_kick()
                    elif game.phase in GAME_INTERRUPTIONS:
                        info(f'Invalidated direct score in {goal} goal from {GAME_INTERRUPTIONS[game.phase]}.')
                        if not right_way:  # own_goal
                            corner_kick(left_side=scoring_team != game.side_left)
                        else:
                            goal_kick()

                elif not right_way and not game.can_score_own:
                    if game.phase == 'KICKOFF':
                        info(f'Invalidated direct score in {goal} goal from kick-off position.')
                    elif game.phase in GAME_INTERRUPTIONS:
                        info(f'Invalidated direct score in {goal} goal from {GAME_INTERRUPTIONS[game.phase]}.')
                    corner_kick(left_side=scoring_team != game.side_left)

                elif game.state.teams[i].players[game.ball_last_touch_player_number - 1].secs_till_unpenalized == 0:
                    game_controller_send(f'SCORE:{scoring_team}')
                    info(f'Score in {goal} goal by {game.ball_last_touch_team} player {game.ball_last_touch_player_number}')
                    game.ready_countdown = SIMULATED_TIME_INTERRUPTION_PHASE_0
                    if game.penalty_shootout:
                        game.interruption_countdown = SIMULATED_TIME_INTERRUPTION_PHASE_0
                        game.penalty_shootout_goal = True
                    else:
                        kickoff()
                elif not right_way:  # own goal
                    game_controller_send(f'SCORE:{scoring_team}')
                    info(f'Score in {goal} goal by {game.ball_last_touch_team} player ' +
                         f'{game.ball_last_touch_player_number} (own goal)')
                    game.ready_countdown = SIMULATED_TIME_INTERRUPTION_PHASE_0
                    if game.penalty_shootout:
                        game.interruption_countdown = SIMULATED_TIME_INTERRUPTION_PHASE_0
                        game.penalty_shootout_goal = True
                    else:
                        kickoff()
                else:
                    info(f'Invalidated score in {goal} goal by penalized ' +
                         f'{game.ball_last_touch_team} player {game.ball_last_touch_player_number}')
                    if game.penalty_shootout:
                        game.interruption_countdown = SIMULATED_TIME_INTERRUPTION_PHASE_0
                        game.penalty_shootout_goal = False
                    else:
                        goal_kick()
    elif game.state.game_state == 'STATE_READY':
        game.play_countdown = 0
        # the GameController will automatically change to the SET state once the state READY is over
        # the referee should wait a little time since the state SET started before sending the PLAY state
    elif game.state.game_state == 'STATE_SET':
        if game.play_countdown == 0 and game.ball_set_kick:
            game.play_countdown = SIMULATED_TIME_BEFORE_PLAY_STATE
            game.ball.resetPhysics()
            game.ball_translation.setSFVec3f(game.ball_kick_translation)
            game.ball_set_kick = False
        else:
            if not game.penalty_shootout:
                if game.dropped_ball:
                    check_dropped_ball_position()
                else:
                    check_kickoff_position()
            game.play_countdown -= 1
            if game.play_countdown == 0:
                game.ready_countdown = 0
                send_play_state_after_penalties = True
    elif game.state.game_state == 'STATE_FINISHED':
        game.sent_finish = False
        if game.penalty_shootout:
            if game.state.seconds_remaining <= 0:
                game.interruption_countdown = SIMULATED_TIME_INTERRUPTION_PHASE_0
                game.penalty_shootout_goal = False
        elif game.state.first_half:
            if game.ready_countdown == 0:
                if game.overtime:
                    type = 'knockout '
                    game_controller_send('STATE:OVERTIME-SECOND-HALF')
                else:
                    type = ''
                    game_controller_send('STATE:SECOND-HALF')
                info(f'Beginning of {type}second half.')
                game.ready_countdown = int(HALF_TIME_BREAK_SIMULATED_DURATION * game.real_time_multiplier)
        elif game.type == 'KNOCKOUT' and game.overtime and game.state.teams[0].score == game.state.teams[1].score:
            if game.ready_countdown == 0:
                info('Beginning of the knockout first half.')
                game_controller_send('STATE:OVERTIME-FIRST-HALF')
                game.ready_countdown = int(HALF_TIME_BREAK_SIMULATED_DURATION * game.real_time_multiplier)
        elif game.type == 'KNOCKOUT' and game.state.teams[0].score == game.state.teams[1].score:
            if game.ready_countdown == 0:
                info('Beginning of penalty shout-out.')
                game_controller_send('STATE:PENALTY-SHOOTOUT')
                game.penalty_shootout = True
                game.ready_countdown = int(HALF_TIME_BREAK_SIMULATED_DURATION * game.real_time_multiplier)
        else:
            game.over = True
            break

    elif game.state.game_state == 'STATE_INITIAL':
        if game.ready_countdown > 0:
            game.ready_countdown -= 1
            if game.ready_countdown == 0:
                if game.penalty_shootout:
                    set_penalty_positions()
                    game_controller_send('STATE:SET')
                    game.play_countdown = SIMULATED_TIME_BEFORE_PLAY_STATE
                else:
                    check_start_position()
                    game_controller_send('STATE:READY')

    if game.interruption_countdown > 0:
        game.interruption_countdown -= 1
        if game.interruption_countdown == 0:
            if game.penalty_shootout:
                next_penalty_shootout()
                if game.over:
                    break
            elif game.ball_set_kick:
                game.ball.resetPhysics()
                game.ball_translation.setSFVec3f(game.ball_kick_translation)
                game.ball_set_kick = False
                info('Ball respawned at '
                     f'{game.ball_kick_translation[0]} {game.ball_kick_translation[1]} {game.ball_kick_translation[2]}.')
            if game.interruption:
                game_controller_send(f'{game.interruption}:{game.interruption_team}:READY')

    check_fallen()                                # detect fallen robots

    if not game.interruption:
        ball_holding = check_ball_holding()       # check for ball holding fouls
        if ball_holding:
            interruption('DIRECT_FREEKICK', ball_holding)

    check_penalized_in_field()                    # check for penalized robots inside the field
    if game.state.game_state != 'STATE_INITIAL':  # send penalties if needed
        send_penalties()
        if send_play_state_after_penalties:
            game_controller_send('STATE:PLAY')
            send_play_state_after_penalties = False

    time_count += time_step

    if game.minimum_real_time_factor != 0:
        # slow down the simulation to guarantee a miminum amount of real time between each step
        t = time.time()
        delta_time = previous_real_time - t + game.minimum_real_time_factor * time_step / 1000
        if delta_time > 0:
            time.sleep(delta_time)
        previous_real_time = time.time()

if not game.over:  # for some reason, the simulation was terminated before the end of the match (may happen during tests)
    info('Game interrupted before the end.')
else:
    info('End of the game.')
    if game.state.teams[0].score > game.state.teams[1].score:
        winner = 0
        loser = 1
    else:
        winner = 1
        loser = 0
    info(f'The score is {game.state.teams[winner].score}-{game.state.teams[loser].score}.')
    if game.state.teams[0].score != game.state.teams[1].score:
        info(f'The winner is the {game.state.teams[winner].team_color.lower()} team.')
    elif game.penalty_shootout_count < 20:
        info('This is a draw.')
    else:  # extended penatly shoutout rules to determine the winner
        count = [0, 0]
        for i in range(5):
            if game.penalty_shootout_time_to_reach_goal_area[2 * i] is not None:
                count[0] += 1
            if game.penalty_shootout_time_to_reach_goal_area[2 * i + 1] is not None:
                count[1] += 1
        if game.kickoff == game.red.id:
            count_red = count[0]
            count_blue = count[1]
        else:
            count_red = count[1]
            count_blue = count[0]
        info('The during the extended penalty shootout, ' +
             f'the ball reached the red goal area {count_blue} times and the blue goal area {count_red} times.')
        if count_red > count_blue:
            info('The winner is the red team.')
        elif count_blue > count_red:
            info('The winner is the blue team.')
        else:
            count = [0, 0]
            for i in range(5):
                if game.penalty_shootout_time_to_touch_ball[2 * i] is not None:
                    count[0] += 1
                if game.penalty_shootout_time_to_touch_ball[2 * i + 1] is not None:
                    count[1] += 1
            if game.kickoff == game.red.id:
                count_red = count[0]
                count_blue = count[1]
            else:
                count_red = count[1]
                count_blue = count[0]
            info(f'The ball was touched {count_red} times by the red player and {count_blue} times by the blue player.')
            if count_red > count_blue:
                info('The winner is the red team.')
            elif count_blue > count_red:
                info('The winner is the blue team.')
            else:
                sum = [0, 0]
                for i in range(5):
                    t = game.penalty_shootout_time_to_score[2 * i]
                    sum[0] += 60 if t is None else t
                    t = game.penalty_shootout_time_to_score[2 * i + 1]
                    sum[1] += 60 if t is None else t
                if game.kickoff == game.red.id:
                    sum_red = sum[0]
                    sum_blue = sum[1]
                else:
                    sum_red = sum[1]
                    sum_blue = sum[0]
                info(f'The red team took {sum_red} seconds to score while blue team took {sum_blue} seconds.')
                if sum_blue < sum_red:
                    info('The winner is the blue team.')
                elif sum_red < sum_blue:
                    info('The winner is the red team.')
                else:
                    sum = [0, 0]
                    for i in range(5):
                        t = game.penalty_shootout_time_to_reach_goal_area[2 * i]
                        sum[0] += 60 if t is None else t
                        t = game.penalty_shootout_time_to_reach_goal_area[2 * i + 1]
                        sum[1] += 60 if t is None else t
                    if game.kickoff == game.red.id:
                        sum_red = sum[0]
                        sum_blue = sum[1]
                    else:
                        sum_red = sum[1]
                        sum_blue = sum[0]
                    info(f'The red team took {sum_red} seconds to send the ball to the goal area ' +
                         f'while blue team took {sum_blue} seconds.')
                    if sum_blue < sum_red:
                        info('The winner is the blue team.')
                    elif sum_red < sum_blue:
                        info('The winner is the red team.')
                    else:
                        sum = [0, 0]
                        for i in range(5):
                            t = game.penalty_shootout_time_to_touch_ball[2 * i]
                            sum[0] += 60 if t is None else t
                            t = game.penalty_shootout_time_to_touch_ball[2 * i + 1]
                            sum[1] += 60 if t is None else t
                        if game.kickoff == game.red.id:
                            sum_red = sum[0]
                            sum_blue = sum[1]
                        else:
                            sum_red = sum[1]
                            sum_blue = sum[0]
                        info(f'The red team took {sum_red} seconds to touch the ball ' +
                             f'while blue team took {sum_blue} seconds.')
                        if sum_blue < sum_red:
                            info('The winner is the blue team.')
                        elif sum_red < sum_blue:
                            info('The winner is the red team.')
                        else:
                            info('Tossing a coin to determine the winner.')
                            if bool(random.getrandbits(1)):
                                info('The winer is the red team.')
                            else:
                                info('The winer is the blue team.')

if log_file:
    log_file.close()
if game.controller:
    game.controller.close()
if game.controller_process:
    game.controller_process.terminate()

if hasattr(game, 'record_simulation'):
    supervisor.animationStopRecording()

if game.over and game.press_a_key_to_terminate:
    print('Press a key to terminate')
    keyboard = supervisor.getKeyboard()
    keyboard.enable(time_step)
    while supervisor.step(time_step) != -1:
        if keyboard.getKey() != -1:
            break

supervisor.simulationQuit(0)
while supervisor.step(time_step) != -1:
    pass
