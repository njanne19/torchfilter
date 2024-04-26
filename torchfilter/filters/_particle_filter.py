"""Private module; avoid importing from directly.
"""

from typing import Optional

import fannypack
import numpy as np
import torch
from overrides import overrides

from .. import types
from ..base import DynamicsModel, Filter, ParticleFilterMeasurementModel
torch.autograd.set_detect_anomaly(True)


class ParticleFilter(Filter):
    """Generic differentiable particle filter."""

    def __init__(
        self,
        *,
        dynamics_model: DynamicsModel,
        measurement_model: ParticleFilterMeasurementModel,
        num_particles: int = 100,
        resample: Optional[bool] = None,
        soft_resample_alpha: float = 1.0,
        estimation_method: str = "weighted_average",
    ):
        # Check submodule consistency
        assert isinstance(dynamics_model, DynamicsModel)
        assert isinstance(measurement_model, ParticleFilterMeasurementModel)
        assert dynamics_model.state_dim == measurement_model.state_dim

        # Initialize state dimension
        state_dim = dynamics_model.state_dim
        super().__init__(state_dim=state_dim)

        # Assign submodules
        self.dynamics_model = dynamics_model
        """torchfilter.base.DynamicsModel: Forward model."""
        self.measurement_model = measurement_model
        """torchfilter.base.ParticleFilterMeasurementModel: Observation model."""

        # Settings
        self.num_particles = num_particles
        """int: Number of particles to represent our belief distribution.
        Defaults to 100."""
        self.resample = resample
        """bool: If True, we resample particles & normalize weights at each
        timestep. If unset (None), we automatically turn resampling on in eval mode
        and off in train mode."""

        self.soft_resample_alpha = soft_resample_alpha
        """float: Tunable constant for differentiable resampling, as described
        by Karkus et al. in "Particle Filter Networks with Application to Visual
        Localization": https://arxiv.org/abs/1805.08975
        Defaults to 1.0 (disabled)."""

        assert estimation_method in ("weighted_average", "argmax")
        self.estimation_method = estimation_method
        """str: Method of producing state estimates. Options include:
        - 'weighted_average': average of particles weighted by their weights.
        - 'argmax': state of highest weighted particle.
        """

        # "Hidden state" tensors
        self.particle_states: torch.Tensor
        """torch.Tensor: Discrete particles representing our current belief
        distribution. Shape should be `(N, M, state_dim)`.
        """
        self.particle_log_weights: torch.Tensor
        """torch.Tensor: Weights corresponding to each particle, stored as
        log-likelihoods. Shape should be `(N, M)`.
        """
        self._initialized = False

    @overrides
    def initialize_beliefs(
        self, *, mean: types.StatesTorch, covariance: types.CovarianceTorch
    ) -> None:
        """Populates initial particles, which will be normally distributed.

        Args:
            mean (torch.Tensor): Mean of belief. Shape should be
                `(N, state_dim)`.
            covariance (torch.Tensor): Covariance of belief. Shape should be
                `(N, state_dim, state_dim)`.
        """
        N = mean.shape[0]
        assert mean.shape == (N, self.state_dim)
        assert covariance.shape == (N, self.state_dim, self.state_dim)
        M = self.num_particles

        # Sample particles
        self.particle_states = (
            torch.distributions.MultivariateNormal(mean, covariance)
            .sample((M,))
            .transpose(0, 1)
        )
        print(f"Particles initialized with version : {self.particle_states._version}")
        assert self.particle_states.shape == (N, M, self.state_dim)

        # Normalize weights
        self.particle_log_weights = self.particle_states.new_full(
            (N, M), float(-np.log(M, dtype=np.float32))
        )
        assert self.particle_log_weights.shape == (N, M)

        # Set initialized flag
        self._initialized = True

    @overrides
    def forward(
        self,
        *,
        observations: types.ObservationsTorch,
        controls: types.ControlsTorch,
    ) -> types.StatesTorch:
        """Particle filter forward pass, single timestep.

        Args:
            observations (dict or torch.Tensor): observation inputs. should be
                either a dict of tensors or tensor of shape `(N, ...)`.
            controls (dict or torch.Tensor): control inputs. should be either a
                dict of tensors or tensor of shape `(N, ...)`.

        Returns:
            torch.Tensor: Predicted state for each batch element. Shape should
            be `(N, state_dim).`
        """

        # Make sure our particle filter's been initialized
        assert self._initialized, "Particle filter not initialized!"

        print(f"At beginning of forward, particles have version : {self.particle_states._version}")

        # Get our batch size (N), current particle count (M), & state dimension
        N, M, state_dim = self.particle_states.shape
        assert state_dim == self.state_dim
        assert len(fannypack.utils.SliceWrapper(controls)) == N

        # Decide whether or not we're resampling
        resample = self.resample
        if resample is None:
            # If not explicitly set, we disable resampling in train mode (to allow
            # gradients to propagate through time) and enable in eval mode (to prevent
            # particle deprivation)
            resample = not self.training

        # If we're not resampling and our current particle count doesn't match
        # our desired particle count, we need to either expand or contract our
        # particle set
        if not resample and self.num_particles != M:
            indices = self.particle_states.new_zeros(
                (N, self.num_particles), dtype=torch.long
            )

            # If output particles > our input particles, for the beginning part we copy
            # particles directly to reduce variance
            copy_count = (self.num_particles // M) * M
            if copy_count > 0:
                indices[:, :copy_count] = torch.arange(M).repeat(copy_count // M)[
                    None, :
                ]

            # For remaining particles, we sample w/o replacement (also lowers variance)
            remaining_count = self.num_particles - copy_count
            assert remaining_count >= 0
            if remaining_count > 0:
                indices[:, copy_count:] = torch.randperm(M, device=indices.device)[
                    None, :remaining_count
                ]

            # Gather new particles, weights
            M = self.num_particles
            self.particle_states = self.particle_states.gather(
                1, indices[:, :, None].expand((N, M, state_dim))
            )
            self.particle_log_weights = self.particle_log_weights.gather(1, indices)
            assert self.particle_states.shape == (N, self.num_particles, state_dim)
            assert self.particle_log_weights.shape == (N, self.num_particles)

            # Normalize particle weights to sum to 1.0
            self.particle_log_weights = self.particle_log_weights - torch.logsumexp(
                self.particle_log_weights, dim=1, keepdim=True
            )

        # Propagate particles through our dynamics model
        # A bit of extra effort is required for the extra particle dimension
        # > For our states, we flatten along the N/M axes
        # > For our controls, we repeat each one `M` times, if M=3:
        #       [u0 u1 u2] should become [u0 u0 u0 u1 u1 u1 u2 u2 u2]
        #
        # Currently each of the M particles within a "sample" get the same action, but
        # we could also add noise in the action space (a la Jonschkowski et al. 2018)
        reshaped_states = self.particle_states.reshape(-1, self.state_dim)
        reshaped_controls = fannypack.utils.SliceWrapper(controls).map(
            lambda tensor: torch.repeat_interleave(tensor, repeats=M, dim=0)
        )
        predicted_states, scale_trils = self.dynamics_model(
            initial_states=reshaped_states, controls=reshaped_controls
        )
        self.particle_states = (
            torch.distributions.MultivariateNormal(
                loc=predicted_states, scale_tril=scale_trils
            )
            .rsample()  # Note that we use `rsample` to make sampling differentiable
            .view(N, M, self.state_dim)
        )
        assert self.particle_states.shape == (N, M, self.state_dim)

        print(f"After resampling, particles have version : {self.particle_states._version}")

        # Re-weight particles using observations
        self.particle_log_weights = self.particle_log_weights + self.measurement_model(
            states=self.particle_states,
            observations=observations,
        )

        # Normalize particle weights to sum to 1.0
        self.particle_log_weights = self.particle_log_weights - torch.logsumexp(
            self.particle_log_weights, dim=1, keepdim=True
        )

        print(f"After re-weighting, particles have version : {self.particle_states._version}")

        # Compute output
        state_estimates: types.StatesTorch
        if self.estimation_method == "weighted_average":

            # First we expand our particle weights to be 3D
            expanded_weights = self.particle_log_weights.unsqueeze(2)

            print(f"Expanded weights have version : {expanded_weights._version}")

            # Then we multiply each particle against its state 
            weighted_states = self.particle_states * torch.exp(expanded_weights)

            print(f"Weighted states have version : {weighted_states._version}")
            print(f"Particle states have version : {self.particle_states._version}")

            state_estimates = torch.sum(weighted_states, dim=1) 
        elif self.estimation_method == "argmax":
            best_indices = torch.argmax(self.particle_log_weights, dim=1)
            state_estimates = torch.gather(
                self.particle_states, dim=1, index=best_indices
            )
        else:
            assert False, "Unsupported estimation method!"

        # Resampling
        if resample:
            self._resample()

        # Post-condition :)
        assert state_estimates.shape == (N, state_dim)
        assert self.particle_states.shape == (N, self.num_particles, state_dim)
        assert self.particle_log_weights.shape == (N, self.num_particles)

        return state_estimates

    def _resample(self) -> None:
        """Resample particles."""
        # Note the distinction between `M`, the current number of particles, and
        # `self.num_particles`, the desired number of particles
        N, M, state_dim = self.particle_states.shape

        sample_logits: torch.Tensor
        uniform_log_weights = self.particle_log_weights.new_full(
            (N, self.num_particles), float(-np.log(M, dtype=np.float32))
        )
        if self.soft_resample_alpha < 1.0:
            # Soft resampling
            assert self.particle_log_weights.shape == (N, M)
            sample_logits = torch.logsumexp(
                torch.stack(
                    [
                        self.particle_log_weights + np.log(self.soft_resample_alpha),
                        uniform_log_weights + np.log(1.0 - self.soft_resample_alpha),
                    ],
                    dim=0,
                ),
                dim=0,
            )
            self.particle_log_weights = self.particle_log_weights - sample_logits
        else:
            # Standard particle filter re-sampling -- this stops gradients
            # This is the most naive flavor of resampling, and not the low
            # variance approach
            #
            # Note the distinction between M, the current # of particles,
            # and self.num_particles, the desired # of particles
            sample_logits = self.particle_log_weights
            self.particle_log_weights = uniform_log_weights

        assert sample_logits.shape == (N, M)
        distribution = torch.distributions.Categorical(logits=sample_logits)
        state_indices = distribution.sample((self.num_particles,)).T
        assert state_indices.shape == (N, self.num_particles)

        self.particle_states = torch.gather(
            self.particle_states,
            dim=1,
            index=state_indices[:, :, None].expand((N, self.num_particles, state_dim)),
        )
        # # ^This gather magic is equivalent to:
        # particle_states_alt = torch.zeros_like(self.particle_states)
        # for i in range(N):
        #     particle_states_alt[i] = self.particle_states[i][state_indices[i]]
