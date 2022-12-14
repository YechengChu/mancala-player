import datetime
import os.path
import random
import time
from pathlib import Path

import numpy as np
import torch
import torch.distributions as dist
from torch.functional import F

from agents.agent import Agent
from agents.alpha_pruning_agent import AlphaPruningAgent
from game.mancalaenv import MancalaEnv
from .config import Config


def get_time_elapsed(start):
    return str(datetime.timedelta(seconds=int(time.time() - start)))


def seed_random(seed):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed(seed)


def get_model_dir(config: Config, epoch, opponent=False):
    if not opponent:
        return f'{config.log_dir}/{config.time_tag}_{epoch}.{config.model_suffix}'
    else:
        return f'{config.log_dir}/{config.time_tag}_{epoch}.opp.{config.model_suffix}'


def load_state_dict_if_exists(config: Config, my_model, opp_model):
    if opp_model is not None:
        for model, opp in zip([my_model, opp_model], [False, True]):
            model_dir = get_model_dir(config, epoch=config.start_epoch, opponent=opp)
            if os.path.isfile(model_dir):
                print(f'existing model found in {model_dir}')
                print(model.load_state_dict(torch.load(model_dir)))
    else:
        model_dir = get_model_dir(config, epoch=config.start_epoch)
        if os.path.isfile(model_dir):
            print(f'existing model found in {model_dir}')
            print(my_model.load_state_dict(torch.load(model_dir)))


def calculate_loss_gae(rewards, log_probabilities, values, entropies, config):
    if len(rewards) <= 0:
        return None
    R = torch.zeros(1, 1).type(torch.float).to(config.device)
    gae = torch.zeros(1, 1).type(torch.float).to(config.device)
    values.append(R.detach().clone().to(config.device).type(torch.float))
    policy_loss = 0
    value_loss = 0
    tau = 1.0
    beta = 0.01
    for i in reversed(range(len(rewards))):
        advantage = R - values[i].to(config.device)
        value_loss = value_loss + 0.5 * advantage.pow(2)

        delta_t = rewards[i] + config.reward_discount * values[i + 1].data - values[i].data
        gae = gae * config.reward_discount * tau + delta_t
        policy_loss = policy_loss - log_probabilities[i] * gae - beta * entropies[i]

    return policy_loss + 0.5 * value_loss


def calculate_loss(rewards, log_probabilities, values, entropies, config):
    # print(f'rewards: {rewards}, rewards type: {type(rewards)}')
    # print(f'log_probabilities: {log_probabilities}')
    # print(f'values: {values}')

    if len(rewards) <= 0:
        return None

    discounted_rewards = []
    accumulated_rewards = 0
    for current_reward in rewards[::-1]:
        accumulated_rewards = config.reward_discount * accumulated_rewards + current_reward
        discounted_rewards.append(accumulated_rewards)

    # print(f'discounted rewards: {discounted_rewards}')

    discounted_rewards = torch.tensor(discounted_rewards[::-1]).float().to(config.device)
    unbiased = True if len(discounted_rewards) > 1 else False
    # print(f'unbiased: {unbiased}')
    # print(f'std+eps rewards: std: {discounted_rewards.std(unbiased=unbiased)},
    # {(discounted_rewards.std(unbiased=unbiased) + config.eps)}')
    normalized_rewards = (discounted_rewards - discounted_rewards.mean()) / \
                         (discounted_rewards.std(unbiased=unbiased) + config.eps)

    # print(f'normalized_rewards: {normalized_rewards}')

    policy_loss = []
    value_loss = []
    for reward, log_probability, value in zip(normalized_rewards, log_probabilities, values):
        policy_loss.append((reward - value) * -log_probability)
        value = value.squeeze(0).squeeze(0)
        value_loss.append(F.smooth_l1_loss(value, reward))

    # print(f'policy_loss: {policy_loss}')
    # print(f'value_loss: {value_loss}')

    return torch.stack(policy_loss).sum() + 0.5 * torch.stack(value_loss).sum()


from copy import deepcopy


# def get_model_input(env, side):
#     board = deepcopy(env.board)
#     if side == 'south':
#         board = np.concatenate((board[len(board) // 2:], board[:len(board) // 2]))
#     # holes = np.concatenate((env.get_holes(side), env.get_holes(env.get_opponent_side(side))))
#     # holes -= np.amin(holes)
#     # holes = holes / (np.amax(holes) + 1e-9)
#     return torch.tensor(board, dtype=torch.float).unsqueeze(0)


def get_model_input(env, side):
    holes = np.concatenate((env.get_holes(side), env.get_holes(env.get_opponent_side(side))))
    # holes -= np.amin(holes)
    # holes = holes / (np.amax(holes) + 1e-9)
    return torch.tensor(holes, dtype=torch.float).unsqueeze(0)

# def to_one_hot(arr):
#     one_hots = None
#     max_size = np.amax(arr) + 1
#     for element in arr:
#         one_hot = np.zeros(max_size, dtype=np.float)
#         one_hot[element] = 1
#         one_hots = one_hot if one_hots is None else np.vstack((one_hots, one_hot))
#     return one_hots


def select_action(env, model, side, hidden, config: Config):
    x = get_model_input(env, side).to(config.device).float()
    output, value, hidden = model.train().to(config.device)(x, hidden)
    distribution = dist.Categorical(F.softmax(output, dim=-1))
    action = distribution.sample()
    log_prob = distribution.log_prob(action)
    entropy = -(log_prob * output).sum(-1)
    return log_prob, action.item() + 1, value, hidden, entropy


def init_hidden(hidden_size, device):
    hx = torch.zeros((1, hidden_size), dtype=torch.float).to(device)
    cx = torch.zeros((1, hidden_size), dtype=torch.float).to(device)
    return hx, cx


def select_action_and_step(env, model, side, hidden, config, rewards, log_probabilities, values, entropies):
    log_prob, action, value, hidden, entropy = select_action(env, model, side, hidden, config)
    next_player, reward, done = env.step(side, action)
    # print(f'side={side}, done={done}, action={action}, reward={reward}')
    rewards.append(reward)
    log_probabilities.append(log_prob)
    values.append(value)
    entropies.append(entropy)

    return done, next_player, hidden


def backward_and_step(model, optimizer, rewards, log_probabilities, values, entropies, config, loss):
    optimizer.zero_grad()
    total_loss = loss(rewards, log_probabilities, values, entropies, config)
    if total_loss is not None:
        total_loss.backward()
        if config.max_clip_grad is not None:
            torch.nn.utils.clip_grad_norm_(model.parameters(), config.max_clip_grad)
        optimizer.step()
        return total_loss.detach().item()
    return None
    # if total_loss > 0:
    #     total_loss.backward()
    #     torch.nn.utils.clip_grad_norm_(model.parameters(), config.max_clip_grad)
    #     optimizer.step()
    #     return total_loss.detach()
    # else:
    #     print(f'nan loss encountered')
    #     return -1


def train_one_game_self_play(config: Config, my_model, opp_model, my_optimizer, opp_optimizer):
    env = MancalaEnv(config.n_holes, config.n_stones)
    my_rewards = []
    my_values = []
    my_log_probabilities = []
    my_entropies = []
    my_hidden = init_hidden(config.hidden_size, config.device)

    opp_rewards = []
    opp_values = []
    opp_log_probabilities = []
    opp_entropies = []
    opp_hidden = init_hidden(config.hidden_size, config.device)

    game_step = 0
    game_finished = False
    next_player = random.choice(['north', 'south'])
    while not game_finished:
        if next_player == 'north':
            # my model move
            done, next_player, my_hidden = select_action_and_step(env, my_model, 'north', my_hidden, config,
                                                                  my_rewards, my_log_probabilities, my_values,
                                                                  my_entropies)
        else:
            # opponent model move
            done, next_player, opp_hidden = select_action_and_step(env, opp_model, 'south', opp_hidden, config,
                                                                   opp_rewards, opp_log_probabilities, opp_values,
                                                                   opp_entropies)
        game_step += 1
        if done or game_step > config.max_game_length:
            game_finished = True

    # in case model wrong move at beginning
    my_total_loss = backward_and_step(my_model, my_optimizer, my_rewards, my_log_probabilities,
                                      my_values, my_entropies, config, loss=calculate_loss)
    opp_total_loss = backward_and_step(opp_model, opp_optimizer, opp_rewards, opp_log_probabilities,
                                       opp_values, opp_entropies, config, loss=calculate_loss)

    return my_total_loss, opp_total_loss


def create_models(config: Config):
    def create():
        return config.model_cls(n_inputs=config.n_inputs, n_outputs=config.n_outputs,
                                hidden_size=config.hidden_size, neuron_size=config.neuron_size)

    return create(), create()


def save_models(config, epoch, my_model, opp_model):
    model_save_dir = get_model_dir(config, epoch=epoch, opponent=False)
    opp_model_save_dir = get_model_dir(config, epoch=epoch, opponent=True)
    print(f'Saving model: epoch={epoch} => {model_save_dir}')
    torch.save(my_model.state_dict(), model_save_dir)
    if opp_model is not None:
        print(f'Saving model: epoch={epoch} => {opp_model_save_dir}')
        torch.save(opp_model.state_dict(), opp_model_save_dir)


def create_log_dir(config: Config):
    Path(config.log_dir).mkdir(exist_ok=True)


def train_self_play(config: Config):
    start_time = time.time()
    seed_random(config.seed)

    create_log_dir(config)

    my_model, opp_model = create_models(config)
    load_state_dict_if_exists(config, my_model, opp_model)
    my_optimizer = config.optimizer_cls(my_model.parameters(), lr=config.lr)
    opp_optimizer = config.optimizer_cls(opp_model.parameters(), lr=config.lr)
    if config.scheduler_cls is not None:
        my_lr_scheduler = config.scheduler_cls(my_optimizer, step_size=config.scheduler_step_size,
                                               gamma=config.scheduler_decay)
        opp_lr_scheduler = config.scheduler_cls(opp_optimizer, step_size=config.scheduler_step_size,
                                                gamma=config.scheduler_decay)
    else:
        my_lr_scheduler = None
        opp_lr_scheduler = None

    print(f'Training for Time Tag: {config.time_tag} has started')

    my_not_backwards = 0
    opp_not_backwards = 0
    last_print_epoch_time = time.time()
    for i in range(config.start_epoch, config.end_epochs):
        this_time = time.time()
        if (this_time - last_print_epoch_time) > 5:  # print every 5 seconds
            print(f'Current Epoch: {i}', end='\r')
            last_print_epoch_time = this_time

        my_loss, opp_loss = train_one_game_self_play(config, my_model, opp_model, my_optimizer, opp_optimizer)

        if my_lr_scheduler is not None:
            my_lr_scheduler.step()
        if opp_lr_scheduler is not None:
            opp_lr_scheduler.step()

        if my_loss is None:
            my_not_backwards += 1
            my_loss = -1
        if opp_loss is None:
            opp_not_backwards += 1
            opp_loss = -1

        if i % config.print_interval == 0:
            print(f'epoch={i:8d} my_loss={my_loss:6f} opp_loss={opp_loss:6f} elapsed={get_time_elapsed(start_time)}')
            my_not_backwards = 0
            opp_not_backwards = 0

        if i % config.save_interval == 0:
            save_models(config, i, my_model, opp_model)

        if i % config.evaluate_interval == 0 and config.evaluation_hook is not None:
            config.evaluation_hook(my_model, opp_model, config)

    save_models(config, config.end_epochs, my_model, opp_model)
    print(f'Finished Training, total time take: {get_time_elapsed(start_time)}')


def train_one_game(config: Config, my_model, my_optimizer, opponent_cls: Agent.__class__):
    opponent = opponent_cls()
    env = MancalaEnv(config.n_holes, config.n_stones)
    my_rewards = []
    my_values = []
    my_log_probabilities = []
    my_entropies = []
    my_hidden = init_hidden(config.hidden_size, config.device)

    game_step = 0
    game_finished = False
    next_player = random.choice(['north', 'south'])
    while not game_finished:
        if next_player == 'north':
            # my model move
            log_prob, action, value, hidden, entropy = select_action(env, my_model, 'north', my_hidden, config)
            next_player, reward, done = env.step('north', action)
            # print(f'side={side}, done={done}, action={action}, reward={reward}')
            my_rewards.append(reward)
            my_log_probabilities.append(log_prob)
            my_values.append(value)
            my_entropies.append(entropy)
        else:
            # opponent move
            action = opponent.get_move(env, 'south')
            next_player, _, done = env.step('south', action)

        game_step += 1
        if done or game_step > config.max_game_length:
            game_finished = True

    # in case model wrong move at beginning
    my_total_loss = backward_and_step(my_model, my_optimizer, my_rewards, my_log_probabilities,
                                      my_values, my_entropies, config, loss=calculate_loss)

    return my_total_loss


def train(config: Config):
    start_time = time.time()
    seed_random(config.seed)

    create_log_dir(config)

    my_model = create_models(config)[0]
    load_state_dict_if_exists(config, my_model, None)
    my_optimizer = config.optimizer_cls(my_model.parameters(), lr=config.lr)
    if config.scheduler_cls is not None:
        my_lr_scheduler = config.scheduler_cls(my_optimizer, step_size=config.scheduler_step_size,
                                               gamma=config.scheduler_decay)
    else:
        my_lr_scheduler = None

    print(f'Training for Time Tag: {config.time_tag} has started')

    last_print_epoch_time = time.time()
    for i in range(config.start_epoch, config.end_epochs):
        this_time = time.time()
        if (this_time - last_print_epoch_time) > 5:  # print every 5 seconds
            print(f'Current Epoch: {i}', end='\r')
            last_print_epoch_time = this_time

        my_loss = train_one_game(config, my_model, my_optimizer, opponent_cls=config.opponent)

        if my_lr_scheduler is not None:
            my_lr_scheduler.step()

        if i % config.print_interval == 0:
            print(f'epoch={i:8d} my_loss={my_loss:6f} elapsed={get_time_elapsed(start_time)}')

        if i % config.save_interval == 0:
            save_models(config, i, my_model, None)

        if i % config.evaluate_interval == 0 and config.evaluation_hook is not None:
            config.evaluation_hook(my_model, None, config)

        if i % config.writer_interval == 0:
            config.writer.add_scalar('loss', my_loss)

    save_models(config, config.end_epochs, my_model, None)
    print(f'Finished Training, total time take: {get_time_elapsed(start_time)}')
