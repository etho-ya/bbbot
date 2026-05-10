"""
Trading Environment — v0.13.1 RL Prototype

Gym-compatible environment for offline RL training on historical position data.
Replays actual position snapshots from PostgreSQL as episodes.

State space (10 features):
  pnl_pct, max_pnl_pct, drawdown_pct, tp_progress_pct, hours_open,
  zone, volatility, volume_ratio, mc_p_tp, mc_p_sl

Action space (4 discrete):
  0 = hold
  1 = partial_close (25%)
  2 = partial_close (50%)
  3 = full_close

Reward:
  - Terminal: capture_ratio = realized_pnl / max_pnl (clamped to [-1, 1])
  - Per-step: -0.001 (small time penalty to discourage infinite holding)
  - Partial close: immediate reward = fraction × current_pnl_pct / 10

Episode:
  One complete historical position (chronological snapshots).
  Episode ends when: full_close action, or no more snapshots.

Usage:
    from core.trading_env import TradingEnv
    env = TradingEnv()
    obs, info = env.reset()
    while True:
        action = agent.predict(obs)
        obs, reward, done, truncated, info = env.step(action)
        if done or truncated:
            break

No external dependencies beyond numpy (gymnasium interface emulated).
"""

import sys
import os
import numpy as np
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger("risk-engine.trading-env")

# Feature indices
FEATURE_NAMES = [
    "pnl_pct", "max_pnl_pct", "drawdown_pct", "tp_progress_pct", "hours_open",
    "zone", "volatility", "volume_ratio", "mc_p_tp", "mc_p_sl",
]
N_FEATURES = len(FEATURE_NAMES)
N_ACTIONS = 4

ACTION_NAMES = ["hold", "partial_close_25", "partial_close_50", "full_close"]


class TradingEnv:
    """
    Gym-compatible Trading Environment for RL training.

    Replays historical position snapshots as episodes.
    Agent decides: hold, partial_close(25%), partial_close(50%), full_close.
    """

    def __init__(self, data_source="csv", csv_path=None, max_steps=500):
        """
        Args:
            data_source: "csv" (from export_ml_dataset.py) or "db" (direct PG query)
            csv_path: path to CSV dataset (default: data/ml_dataset_v1.csv)
            max_steps: max steps per episode before truncation
        """
        self.max_steps = max_steps
        self.episodes = []  # List of episodes: [np.array of shape (T, N_FEATURES)]
        self.episode_meta = []  # [{pos_id, symbol, side, realized_pnl}]

        self._load_data(data_source, csv_path)

        # State
        self._current_episode_idx = 0
        self._current_step = 0
        self._closed_fraction = 0.0
        self._cumulative_reward = 0.0

        # Observation and action space descriptors (gymnasium-compatible)
        self.observation_space_shape = (N_FEATURES,)
        self.action_space_n = N_ACTIONS
        self.n_episodes = len(self.episodes)

        logger.info(
            f"TradingEnv initialized: {self.n_episodes} episodes, "
            f"{N_FEATURES} features, {N_ACTIONS} actions"
        )

    def _load_data(self, data_source, csv_path):
        """Load historical position data grouped by position."""
        if data_source == "csv":
            self._load_from_csv(csv_path)
        elif data_source == "db":
            self._load_from_db()
        else:
            raise ValueError(f"Unknown data_source: {data_source}")

    def _load_from_csv(self, csv_path=None):
        """Load from exported CSV file."""
        import csv as csv_module

        if csv_path is None:
            csv_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "data", "ml_dataset_v1.csv"
            )

        if not os.path.exists(csv_path):
            raise FileNotFoundError(
                f"Dataset not found: {csv_path}\n"
                f"Run: python3 scripts/export_ml_dataset.py"
            )

        # Read CSV and group by pos_id
        positions = {}  # {pos_id: [rows]}
        pos_meta = {}

        with open(csv_path, "r") as f:
            reader = csv_module.DictReader(f)
            for row in reader:
                pos_id = int(row["pos_id"])
                if pos_id not in positions:
                    positions[pos_id] = []
                    pos_meta[pos_id] = {
                        "pos_id": pos_id,
                        "symbol": row.get("symbol", ""),
                        "side": row.get("side", ""),
                    }

                features = []
                for feat in FEATURE_NAMES:
                    try:
                        features.append(float(row.get(feat, 0)))
                    except (ValueError, TypeError):
                        features.append(0.0)
                positions[pos_id].append(features)

        # Convert to numpy arrays
        for pos_id in sorted(positions.keys()):
            snapshots = np.array(positions[pos_id], dtype=np.float32)
            if len(snapshots) >= 3:  # Skip positions with too few snapshots
                self.episodes.append(snapshots)
                self.episode_meta.append(pos_meta[pos_id])

        logger.info(f"Loaded {len(self.episodes)} episodes from CSV ({csv_path})")

    def _load_from_db(self):
        """Load directly from PostgreSQL."""
        from db_adapter import _sync_fetch_all, PG_DB, PG_USER, PG_HOST
        import db_adapter
        import psycopg2.pool

        if db_adapter._pool is None:
            db_adapter._pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=1, maxconn=3,
                dbname=PG_DB, user=PG_USER, host=PG_HOST,
                options="-c jit=off -c statement_timeout=60000",
            )

        rows = _sync_fetch_all("""
            SELECT ps.pos_id, ps.symbol, ps.side,
                   ps.pnl_pct, ps.max_pnl_pct, ps.drawdown_pct,
                   ps.tp_progress_pct, ps.hours_open, ps.zone,
                   ps.volatility, ps.volume_ratio, ps.mc_p_tp, ps.mc_p_sl
            FROM position_snapshots ps
            JOIN open_positions op ON ps.pos_id = op.id
            WHERE op.status = 'closed'
            ORDER BY ps.pos_id, ps.id
        """)

        positions = {}
        pos_meta = {}
        for row in rows:
            pos_id = row["pos_id"]
            if pos_id not in positions:
                positions[pos_id] = []
                pos_meta[pos_id] = {
                    "pos_id": pos_id,
                    "symbol": row["symbol"],
                    "side": row["side"],
                }
            features = [row.get(f) or 0 for f in FEATURE_NAMES]
            positions[pos_id].append(features)

        for pos_id in sorted(positions.keys()):
            snapshots = np.array(positions[pos_id], dtype=np.float32)
            if len(snapshots) >= 3:
                self.episodes.append(snapshots)
                self.episode_meta.append(pos_meta[pos_id])

        logger.info(f"Loaded {len(self.episodes)} episodes from PostgreSQL")

    def reset(self, episode_idx=None, seed=None):
        """
        Reset environment to start of a new episode.

        Args:
            episode_idx: specific episode index, or None for sequential
            seed: random seed (unused, for compatibility)

        Returns:
            observation (np.array), info (dict)
        """
        if episode_idx is not None:
            self._current_episode_idx = episode_idx % self.n_episodes
        else:
            self._current_episode_idx = (self._current_episode_idx + 1) % self.n_episodes

        self._current_step = 0
        self._closed_fraction = 0.0
        self._cumulative_reward = 0.0

        obs = self._get_observation()
        info = {
            "episode_idx": self._current_episode_idx,
            "meta": self.episode_meta[self._current_episode_idx],
            "total_steps": len(self.episodes[self._current_episode_idx]),
        }
        return obs, info

    def step(self, action):
        """
        Execute one step.

        Args:
            action: int in [0, 3] — hold / partial_25 / partial_50 / full_close

        Returns:
            observation, reward, terminated, truncated, info
        """
        assert 0 <= action < N_ACTIONS, f"Invalid action: {action}"

        episode = self.episodes[self._current_episode_idx]
        current_obs = episode[self._current_step]

        pnl_pct = current_obs[0]     # Current PnL
        max_pnl = current_obs[1]     # Max PnL seen

        reward = -0.001  # Time penalty
        terminated = False
        truncated = False
        info = {"action_name": ACTION_NAMES[action]}

        if action == 0:
            # Hold — no action
            pass
        elif action == 1:
            # Partial close 25%
            if self._closed_fraction < 1.0:
                fraction = min(0.25, 1.0 - self._closed_fraction)
                self._closed_fraction += fraction
                reward += fraction * pnl_pct / 10.0  # Immediate partial reward
                info["closed_fraction"] = fraction
        elif action == 2:
            # Partial close 50%
            if self._closed_fraction < 1.0:
                fraction = min(0.50, 1.0 - self._closed_fraction)
                self._closed_fraction += fraction
                reward += fraction * pnl_pct / 10.0
                info["closed_fraction"] = fraction
        elif action == 3:
            # Full close
            remaining = 1.0 - self._closed_fraction
            if remaining > 0:
                reward += remaining * pnl_pct / 10.0
                self._closed_fraction = 1.0
            terminated = True

        # Advance step
        self._current_step += 1
        self._cumulative_reward += reward

        # Check termination conditions
        if self._closed_fraction >= 1.0:
            terminated = True
        if self._current_step >= len(episode):
            truncated = True
            # Terminal reward: capture ratio
            if max_pnl > 0:
                capture = np.clip(pnl_pct / max_pnl, -1.0, 1.0)
                reward += capture * 0.5  # Bonus for good capture
            elif pnl_pct < 0:
                reward -= 0.1  # Penalty for ending in loss

        if terminated or truncated:
            info["cumulative_reward"] = self._cumulative_reward
            info["final_pnl"] = pnl_pct
            info["max_pnl"] = max_pnl
            info["closed_fraction"] = self._closed_fraction

        obs = self._get_observation() if not (terminated or truncated) else np.zeros(N_FEATURES, dtype=np.float32)

        return obs, reward, terminated, truncated, info

    def _get_observation(self):
        """Get current observation vector."""
        episode = self.episodes[self._current_episode_idx]
        if self._current_step < len(episode):
            return episode[self._current_step].copy()
        return np.zeros(N_FEATURES, dtype=np.float32)

    def get_episode_summary(self, episode_idx):
        """Get summary statistics for an episode."""
        episode = self.episodes[episode_idx]
        meta = self.episode_meta[episode_idx]
        return {
            **meta,
            "n_snapshots": len(episode),
            "max_pnl": float(np.max(episode[:, 1])),
            "min_pnl": float(np.min(episode[:, 0])),
            "final_pnl": float(episode[-1, 0]),
            "max_zone": int(np.max(episode[:, 5])),
            "duration_hours": float(episode[-1, 4]),
        }

    def run_random_agent(self, n_episodes=10, seed=42):
        """Run random agent for baseline comparison."""
        rng = np.random.RandomState(seed)
        results = []

        for i in range(n_episodes):
            obs, info = self.reset(episode_idx=i)
            total_reward = 0
            steps = 0

            while True:
                action = rng.randint(0, N_ACTIONS)
                obs, reward, done, trunc, info = self.step(action)
                total_reward += reward
                steps += 1
                if done or trunc:
                    break

            results.append({
                "episode": i,
                "total_reward": total_reward,
                "steps": steps,
                "final_pnl": info.get("final_pnl", 0),
                "max_pnl": info.get("max_pnl", 0),
            })

        avg_reward = np.mean([r["total_reward"] for r in results])
        logger.info(f"Random Agent: {n_episodes} episodes, avg_reward={avg_reward:.4f}")
        return results


def test_env():
    """Quick smoke test of the TradingEnv."""
    env = TradingEnv(data_source="csv")
    print(f"✅ Environment initialized: {env.n_episodes} episodes")
    print(f"   State shape: {env.observation_space_shape}")
    print(f"   Actions: {N_ACTIONS} ({', '.join(ACTION_NAMES)})")

    # Run one episode with hold-only policy
    obs, info = env.reset(episode_idx=0)
    print(f"\n📍 Episode 0: {info['meta']['symbol']} ({info['total_steps']} steps)")
    print(f"   Initial state: pnl={obs[0]:.2f}%, max={obs[1]:.2f}%, zone={int(obs[5])}")

    steps = 0
    while True:
        obs, reward, done, trunc, info = env.step(0)  # Always hold
        steps += 1
        if done or trunc:
            break

    print(f"   Hold-only: {steps} steps, final_pnl={info.get('final_pnl', 0):.2f}%")

    # Run random agent baseline
    print("\n🎲 Random Agent Baseline:")
    results = env.run_random_agent(n_episodes=min(10, env.n_episodes))
    for r in results[:5]:
        print(
            f"   Ep {r['episode']:3d}: reward={r['total_reward']:+.4f}, "
            f"steps={r['steps']:3d}, final_pnl={r['final_pnl']:+.2f}%"
        )

    avg = np.mean([r["total_reward"] for r in results])
    print(f"\n   Average reward: {avg:+.4f}")
    print("✅ TradingEnv smoke test passed")


if __name__ == "__main__":
    test_env()
