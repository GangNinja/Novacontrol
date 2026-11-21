"""A small, dependency-free DQN implementation for PI-15.

The PI-13 action space is discrete (BUY, SELL, HOLD, WAIT, NO_ACTION).  A
compact replay-based DQN is therefore a better fit than a continuous-control
algorithm such as SAC, and avoids introducing a second heavyweight ML runtime.
The implementation is deliberately pure Python so training remains deterministic,
runs on the project's CPU-only target and does not make torch mandatory for the
base install.
"""

from __future__ import annotations

import math
import random
from collections import deque
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

ACTION_NAMES: tuple[str, ...] = ("buy", "sell", "hold", "wait", "no_action")
DEFAULT_SIZE_FRACTION = "0.20"


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _get(value: Mapping[str, Any], path: Sequence[str]) -> Any:
    current: Any = value
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    return current


@dataclass(frozen=True, slots=True)
class ObservationEncoder:
    """Stable numeric observation projection, saved with every policy."""

    feature_names: tuple[str, ...] = ()
    version: str = "pi15-observation-encoder-v1"

    BASE_PATHS: tuple[tuple[str, ...], ...] = (
        ("market", "close"),
        ("market", "return_1"),
        ("market", "return_5"),
        ("market", "return_20"),
        ("market", "gap_pct"),
        ("market", "intraday_range_pct"),
        ("market", "volatility_20"),
        ("market", "relative_volume_20"),
        ("market", "price_vs_sma_20"),
        ("market", "price_vs_sma_50"),
        ("market", "support_distance_pct"),
        ("market", "resistance_distance_pct"),
        ("portfolio", "cash"),
        ("portfolio", "equity"),
        ("portfolio", "exposure"),
        ("portfolio", "drawdown"),
        ("forecasts", "expected_return"),
        ("forecasts", "p_up"),
        ("forecasts", "p_down"),
        ("forecasts", "uncertainty_width"),
        ("forecasts", "disagreement"),
    )
    # Fixed, point-in-time scales.  No statistic is fitted on training data, so
    # validation/test observations cannot influence the encoder and there is no
    # reward-normalisation leakage.  Unknown feature values use a unit tanh.
    BASE_SCALES: tuple[float, ...] = (
        100.0,
        0.05,
        0.10,
        0.30,
        0.05,
        0.05,
        0.50,
        2.0,
        0.10,
        0.20,
        0.05,
        0.05,
        100_000.0,
        100_000.0,
        1.0,
        0.20,
        0.10,
        1.0,
        1.0,
        0.10,
        0.10,
    )

    @classmethod
    def fit(cls, observation: Mapping[str, Any]) -> ObservationEncoder:
        features = _get(observation, ("features", "values"))
        names = tuple(sorted(str(key) for key in features if isinstance(features, Mapping)))
        return cls(feature_names=names)

    @property
    def dimension(self) -> int:
        return len(self.BASE_PATHS) + len(self.feature_names)

    def encode(self, observation: Mapping[str, Any]) -> list[float]:
        values = [
            math.tanh(_number(_get(observation, path)) / scale)
            for path, scale in zip(self.BASE_PATHS, self.BASE_SCALES, strict=True)
        ]
        features = _get(observation, ("features", "values"))
        feature_map: Mapping[str, Any] = features if isinstance(features, Mapping) else {}
        values.extend(
            math.tanh(_number(feature_map.get(name)))
            for name in self.feature_names
        )
        return values

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "feature_names": list(self.feature_names),
            "base_fields": [".".join(path) for path in self.BASE_PATHS],
            "dimension": self.dimension,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ObservationEncoder:
        names = data.get("feature_names")
        return cls(
            feature_names=tuple(str(name) for name in names)
            if isinstance(names, (list, tuple))
            else (),
        )


class DenseNetwork:
    """A deterministic multilayer perceptron with manual backpropagation."""

    def __init__(
        self,
        sizes: Sequence[int],
        *,
        activation: str = "relu",
        rng: random.Random,
    ) -> None:
        if len(sizes) < 2:
            raise ValueError("network needs an input and an output layer")
        self.sizes = tuple(int(size) for size in sizes)
        self.activation = activation
        self.weights: list[list[list[float]]] = []
        self.biases: list[list[float]] = []
        for index, (fan_in, fan_out) in enumerate(zip(self.sizes, self.sizes[1:], strict=False)):
            if activation == "relu":
                scale = math.sqrt(2.0 / max(1, fan_in))
            else:
                scale = math.sqrt(1.0 / max(1, fan_in))
            self.weights.append(
                [[rng.gauss(0.0, scale) for _ in range(fan_in)] for _ in range(fan_out)]
            )
            self.biases.append([0.0 for _ in range(fan_out)])
            if index == len(self.sizes) - 2:
                break

    def _activate(self, value: float) -> float:
        if self.activation == "tanh":
            return math.tanh(value)
        return value if value > 0 else 0.0

    def _derivative(self, value: float, activated: float) -> float:
        if self.activation == "tanh":
            return 1.0 - activated * activated
        return 1.0 if value > 0 else 0.0

    def forward(
        self, inputs: Sequence[float]
    ) -> tuple[list[float], list[list[float]], list[list[float]]]:
        if len(inputs) != self.sizes[0]:
            raise ValueError("network input dimension does not match the encoder")
        activations: list[list[float]] = [list(inputs)]
        z_values: list[list[float]] = []
        current = list(inputs)
        for _layer_index, (weights, biases) in enumerate(
            zip(self.weights, self.biases, strict=True)
        ):
            z_row = [
                sum(weight * value for weight, value in zip(row, current, strict=True)) + bias
                for row, bias in zip(weights, biases, strict=True)
            ]
            z_values.append(z_row)
            current = [self._activate(value) for value in z_row]
            activations.append(current)
        return current, activations, z_values

    def predict(self, inputs: Sequence[float]) -> list[float]:
        output, _activations, _z = self.forward(inputs)
        return output

    def copy(self) -> DenseNetwork:
        clone = object.__new__(DenseNetwork)
        clone.sizes = self.sizes
        clone.activation = self.activation
        clone.weights = [[list(row) for row in layer] for layer in self.weights]
        clone.biases = [list(layer) for layer in self.biases]
        return clone

    def backward(
        self,
        activations: Sequence[Sequence[float]],
        z_values: Sequence[Sequence[float]],
        output_gradient: Sequence[float],
    ) -> tuple[list[list[list[float]]], list[list[float]]]:
        delta = list(output_gradient)
        weight_grads: list[list[list[float]]] = [list(row) for row in self.weights]
        bias_grads: list[list[float]] = [list(row) for row in self.biases]
        for layer_index in range(len(self.weights) - 1, -1, -1):
            previous = activations[layer_index]
            layer_weights = self.weights[layer_index]
            for output_index, gradient in enumerate(delta):
                row = weight_grads[layer_index][output_index]
                for input_index, value in enumerate(previous):
                    row[input_index] = gradient * value
                bias_grads[layer_index][output_index] = gradient
            if layer_index == 0:
                break
            previous_delta = [0.0 for _ in previous]
            for input_index in range(len(previous)):
                total = 0.0
                for output_index, gradient in enumerate(delta):
                    total += layer_weights[output_index][input_index] * gradient
                previous_delta[input_index] = total * self._derivative(
                    z_values[layer_index - 1][input_index],
                    activations[layer_index][input_index],
                )
            delta = previous_delta
        return weight_grads, bias_grads

    def to_dict(self) -> dict[str, Any]:
        return {
            "sizes": list(self.sizes),
            "activation": self.activation,
            "weights": [[list(row) for row in layer] for layer in self.weights],
            "biases": [list(layer) for layer in self.biases],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DenseNetwork:
        sizes = data.get("sizes")
        weights = data.get("weights")
        biases = data.get("biases")
        if not isinstance(sizes, (list, tuple)) or len(sizes) < 2:
            raise ValueError("stored network sizes are invalid")
        clone = object.__new__(cls)
        clone.sizes = tuple(int(size) for size in sizes)
        activation = str(data.get("activation") or "relu")
        clone.activation = activation if activation in {"relu", "tanh"} else "relu"
        if not isinstance(weights, (list, tuple)) or not isinstance(biases, (list, tuple)):
            raise ValueError("stored network parameters are invalid")
        clone.weights = [
            [[float(value) for value in row] for row in layer]
            for layer in weights
        ]
        clone.biases = [[float(value) for value in layer] for layer in biases]
        return clone


@dataclass(frozen=True, slots=True)
class ReplayExperience:
    state: tuple[float, ...]
    action: int
    reward: float
    next_state: tuple[float, ...]
    done: bool


class ReplayBuffer:
    def __init__(self, capacity: int, *, seed: int = 42) -> None:
        self._items: deque[ReplayExperience] = deque(maxlen=max(1, int(capacity)))
        self._rng = random.Random(seed)

    def __len__(self) -> int:
        return len(self._items)

    def add(
        self,
        state: Sequence[float],
        action: int,
        reward: float,
        next_state: Sequence[float],
        done: bool,
    ) -> None:
        self._items.append(
            ReplayExperience(
                tuple(float(value) for value in state),
                int(action),
                float(reward),
                tuple(float(value) for value in next_state),
                bool(done),
            )
        )

    def sample(self, size: int) -> list[ReplayExperience]:
        count = min(max(1, int(size)), len(self._items))
        return self._rng.sample(list(self._items), count)

    def state_dict(self) -> dict[str, Any]:
        return {
            "capacity": self._items.maxlen,
            "items": [
                {
                    "state": list(item.state),
                    "action": item.action,
                    "reward": item.reward,
                    "next_state": list(item.next_state),
                    "done": item.done,
                }
                for item in self._items
            ],
            "rng_state": list(self._rng.getstate()),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReplayBuffer:
        capacity = int(data.get("capacity") or 5_000)
        buffer = cls(capacity)
        rows = data.get("items")
        if isinstance(rows, (list, tuple)):
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                state = row.get("state")
                next_state = row.get("next_state")
                if not isinstance(state, (list, tuple)) or not isinstance(
                    next_state, (list, tuple)
                ):
                    continue
                buffer.add(
                    [float(value) for value in state],
                    int(row.get("action") or 0),
                    float(row.get("reward") or 0.0),
                    [float(value) for value in next_state],
                    bool(row.get("done", False)),
                )
        rng_state = data.get("rng_state")
        if isinstance(rng_state, (list, tuple)):
            with suppress(TypeError, ValueError):
                buffer._rng.setstate(tuple(rng_state))  # noqa: SLF001 - serialized RNG
        return buffer


class DQNAgent:
    """Replay-based deep Q-learning with a target network."""

    def __init__(
        self,
        *,
        input_dim: int,
        action_count: int = len(ACTION_NAMES),
        hidden_layers: Sequence[int] = (64, 32),
        activation: str = "relu",
        optimizer: str = "adam",
        learning_rate: float = 0.001,
        gamma: float = 0.99,
        epsilon_start: float = 1.0,
        epsilon_end: float = 0.05,
        epsilon_decay: float = 0.995,
        target_update_interval: int = 25,
        replay_capacity: int = 5_000,
        seed: int = 42,
        encoder: ObservationEncoder | None = None,
    ) -> None:
        self.input_dim = int(input_dim)
        self.action_count = int(action_count)
        self.hidden_layers = tuple(int(value) for value in hidden_layers)
        self.activation = activation
        self.optimizer = optimizer
        self.learning_rate = float(learning_rate)
        self.gamma = float(gamma)
        self.epsilon_start = float(epsilon_start)
        self.epsilon_end = float(epsilon_end)
        self.epsilon_decay = float(epsilon_decay)
        self.target_update_interval = max(1, int(target_update_interval))
        self.seed = int(seed)
        self.encoder = encoder or ObservationEncoder()
        sizes = [self.input_dim, *self.hidden_layers, self.action_count]
        self._rng = random.Random(self.seed)
        self.online = DenseNetwork(sizes, activation=activation, rng=self._rng)
        self.target = self.online.copy()
        self.replay = ReplayBuffer(replay_capacity, seed=self.seed + 1)
        self.steps = 0
        self._adam_m: list[list[list[float]]] = [
            [[0.0 for _ in row] for row in layer] for layer in self.online.weights
        ]
        self._adam_v: list[list[list[float]]] = [
            [[0.0 for _ in row] for row in layer] for layer in self.online.weights
        ]
        self._adam_bm = [0.0 for _ in self.online.biases]
        self._adam_bv = [0.0 for _ in self.online.biases]

    def epsilon_for_step(self, step: int) -> float:
        value = self.epsilon_start * (self.epsilon_decay ** max(0, int(step)))
        return max(self.epsilon_end, min(self.epsilon_start, value))

    def q_values(self, state: Sequence[float]) -> list[float]:
        return self.online.predict(state)

    def act(self, state: Sequence[float], *, epsilon: float | None = None) -> int:
        exploration = self.epsilon_for_step(self.steps) if epsilon is None else float(epsilon)
        if self._rng.random() < exploration:
            return self._rng.randrange(self.action_count)
        values = self.q_values(state)
        return max(range(len(values)), key=lambda index: values[index])

    def remember(
        self,
        state: Sequence[float],
        action: int,
        reward: float,
        next_state: Sequence[float],
        done: bool,
    ) -> None:
        self.replay.add(state, action, reward, next_state, done)

    def train_step(self, batch_size: int) -> float | None:
        if len(self.replay) < max(1, int(batch_size)):
            return None
        batch = self.replay.sample(batch_size)
        weight_acc = [
            [[0.0 for _ in row] for row in layer] for layer in self.online.weights
        ]
        bias_acc = [[0.0 for _ in layer] for layer in self.online.biases]
        losses: list[float] = []
        for item in batch:
            current_output, activations, z_values = self.online.forward(item.state)
            if item.done:
                target_value = item.reward
            else:
                target_value = item.reward + self.gamma * max(
                    self.target.predict(item.next_state)
                )
            prediction = current_output[item.action]
            error = prediction - target_value
            losses.append(error * error)
            gradient = [0.0 for _ in current_output]
            gradient[item.action] = max(-1.0, min(1.0, error))
            weight_grads, bias_grads = self.online.backward(
                activations, z_values, gradient
            )
            for weight_layer_index, weight_layer in enumerate(weight_grads):
                for weight_row_index, weight_row in enumerate(weight_layer):
                    for weight_value_index, weight_value in enumerate(weight_row):
                        weight_acc[weight_layer_index][weight_row_index][weight_value_index] += (
                            weight_value
                        )
            for bias_layer_index, bias_layer in enumerate(bias_grads):
                for bias_index, bias_value in enumerate(bias_layer):
                    bias_acc[bias_layer_index][bias_index] += bias_value
        scale = 1.0 / len(batch)
        for scaled_weight_layer in weight_acc:
            for scaled_weight_row in scaled_weight_layer:
                for scaled_index in range(len(scaled_weight_row)):
                    scaled_weight_row[scaled_index] *= scale
        for scaled_bias_layer in bias_acc:
            for scaled_bias_index in range(len(scaled_bias_layer)):
                scaled_bias_layer[scaled_bias_index] *= scale
        self._apply_gradients(weight_acc, bias_acc)
        self.steps += 1
        if self.steps % self.target_update_interval == 0:
            self.target = self.online.copy()
        return sum(losses) / len(losses)

    def _apply_gradients(
        self, weight_grads: list[list[list[float]]], bias_grads: list[list[float]]
    ) -> None:
        if self.optimizer == "sgd":
            for weight_layer_index, weight_layer in enumerate(self.online.weights):
                for weight_row_index, weight_row in enumerate(weight_layer):
                    for weight_index, weight_value in enumerate(weight_row):
                        self.online.weights[weight_layer_index][weight_row_index][weight_index] = (
                            weight_value
                            - self.learning_rate
                            * weight_grads[weight_layer_index][weight_row_index][weight_index]
                        )
            for bias_layer_index, bias_layer in enumerate(self.online.biases):
                for bias_index, bias_value in enumerate(bias_layer):
                    self.online.biases[bias_layer_index][bias_index] = (
                        bias_value
                        - self.learning_rate * bias_grads[bias_layer_index][bias_index]
                    )
            return
        beta1, beta2, epsilon = 0.9, 0.999, 1e-8
        step = max(1, self.steps + 1)
        correction1 = 1.0 - beta1**step
        correction2 = 1.0 - beta2**step
        for weight_layer_index, weight_layer in enumerate(self.online.weights):
            for weight_row_index, weight_row in enumerate(weight_layer):
                for weight_index, weight_value in enumerate(weight_row):
                    grad = weight_grads[weight_layer_index][weight_row_index][weight_index]
                    m = (
                        beta1
                        * self._adam_m[weight_layer_index][weight_row_index][weight_index]
                        + (1 - beta1) * grad
                    )
                    v = (
                        beta2
                        * self._adam_v[weight_layer_index][weight_row_index][weight_index]
                        + (1 - beta2) * grad * grad
                    )
                    self._adam_m[weight_layer_index][weight_row_index][weight_index] = m
                    self._adam_v[weight_layer_index][weight_row_index][weight_index] = v
                    update = (m / correction1) / (math.sqrt(v / correction2) + epsilon)
                    self.online.weights[weight_layer_index][weight_row_index][weight_index] = (
                        weight_value - self.learning_rate * update
                    )
        for bias_layer_index, bias_layer in enumerate(self.online.biases):
            for bias_index, bias_value in enumerate(bias_layer):
                grad = bias_grads[bias_layer_index][bias_index]
                m = beta1 * self._adam_bm[bias_layer_index] + (1 - beta1) * grad
                v = beta2 * self._adam_bv[bias_layer_index] + (1 - beta2) * grad * grad
                self._adam_bm[bias_layer_index] = m
                self._adam_bv[bias_layer_index] = v
                update = (m / correction1) / (math.sqrt(v / correction2) + epsilon)
                self.online.biases[bias_layer_index][bias_index] = (
                    bias_value - self.learning_rate * update
                )

    def save_state(self) -> dict[str, Any]:
        return {
            "input_dim": self.input_dim,
            "action_count": self.action_count,
            "hidden_layers": list(self.hidden_layers),
            "activation": self.activation,
            "optimizer": self.optimizer,
            "learning_rate": self.learning_rate,
            "gamma": self.gamma,
            "epsilon_start": self.epsilon_start,
            "epsilon_end": self.epsilon_end,
            "epsilon_decay": self.epsilon_decay,
            "target_update_interval": self.target_update_interval,
            "seed": self.seed,
            "steps": self.steps,
            "encoder": self.encoder.to_dict(),
            "online": self.online.to_dict(),
            "target": self.target.to_dict(),
            "rng_state": list(self._rng.getstate()),
            "replay": self.replay.state_dict(),
            "adam_m": self._adam_m,
            "adam_v": self._adam_v,
            "adam_bm": self._adam_bm,
            "adam_bv": self._adam_bv,
        }

    @classmethod
    def from_state(cls, data: Mapping[str, Any]) -> DQNAgent:
        encoder_data = data.get("encoder")
        encoder = (
            ObservationEncoder.from_dict(encoder_data)
            if isinstance(encoder_data, Mapping)
            else ObservationEncoder()
        )
        agent = cls(
            input_dim=int(data.get("input_dim") or encoder.dimension),
            action_count=int(data.get("action_count") or len(ACTION_NAMES)),
            hidden_layers=tuple(int(value) for value in data.get("hidden_layers") or (64, 32)),
            activation=str(data.get("activation") or "relu"),
            optimizer=str(data.get("optimizer") or "adam"),
            learning_rate=float(data.get("learning_rate") or 0.001),
            gamma=float(data.get("gamma") or 0.99),
            epsilon_start=float(data.get("epsilon_start") or 1.0),
            epsilon_end=float(data.get("epsilon_end") or 0.05),
            epsilon_decay=float(data.get("epsilon_decay") or 0.995),
            target_update_interval=int(data.get("target_update_interval") or 25),
            replay_capacity=int((data.get("replay") or {}).get("capacity") or 5_000),
            seed=int(data.get("seed") or 42),
            encoder=encoder,
        )
        online = data.get("online")
        target = data.get("target")
        if isinstance(online, Mapping):
            agent.online = DenseNetwork.from_dict(online)
        if isinstance(target, Mapping):
            agent.target = DenseNetwork.from_dict(target)
        agent.steps = max(0, int(data.get("steps") or 0))
        replay = data.get("replay")
        if isinstance(replay, Mapping):
            agent.replay = ReplayBuffer.from_dict(replay)
        rng_state = data.get("rng_state")
        if isinstance(rng_state, (list, tuple)):
            with suppress(TypeError, ValueError):
                agent._rng.setstate(tuple(rng_state))  # noqa: SLF001 - serialized RNG
        if isinstance(data.get("adam_m"), list):
            agent._adam_m = data["adam_m"]
        if isinstance(data.get("adam_v"), list):
            agent._adam_v = data["adam_v"]
        if isinstance(data.get("adam_bm"), list):
            agent._adam_bm = [float(value) for value in data["adam_bm"]]
        if isinstance(data.get("adam_bv"), list):
            agent._adam_bv = [float(value) for value in data["adam_bv"]]
        return agent


def action_payload(action_index: int, observation: Mapping[str, Any]) -> dict[str, Any]:
    """Map a discrete policy action to the PI-13 action contract."""

    index = int(action_index) % len(ACTION_NAMES)
    name = ACTION_NAMES[index]
    instrument = str(observation.get("instrument_id") or "").strip().upper()
    if name in {"buy", "sell"} and instrument:
        return {
            "action_type": name,
            "instrument_id": instrument,
            "size_fraction": DEFAULT_SIZE_FRACTION,
        }
    return {"action_type": "wait" if name in {"buy", "sell"} else name}


class TrainedPolicy:
    """PI-14 ``Policy`` adapter over a persisted PI-15 DQN agent.

    The adapter produces advisory actions only.  It has no controller, order
    gateway, risk engine or autonomy handle.
    """

    def __init__(
        self,
        agent: DQNAgent,
        *,
        name: str,
        version: str = "pi15-trained-1",
        description: str = "Experimental PI-15 trained DQN policy; advisory only.",
    ) -> None:
        self.agent = agent
        self.name = name
        self.version = version
        self.description = description

    def select(
        self,
        *,
        observation: Mapping[str, Any],
        portfolio: Mapping[str, Any],
        environment: Mapping[str, Any],
        history: Sequence[Mapping[str, Any]],
    ) -> Any:
        from novacontrol.novamarket.rewards.policies import PolicyDecision

        state = self.agent.encoder.encode(observation)
        action_index = self.agent.act(state, epsilon=0.0)
        return PolicyDecision(
            action_payload(action_index, observation),
            f"PI-15 trained DQN argmax action {ACTION_NAMES[action_index]}; "
            "experimental and advisory only.",
        )


__all__ = [
    "ACTION_NAMES",
    "DQNAgent",
    "DenseNetwork",
    "ObservationEncoder",
    "ReplayBuffer",
    "ReplayExperience",
    "TrainedPolicy",
    "action_payload",
]
