from functools import partial

import chex
import jax.numpy as jnp
import jax.random as jr
import tensorflow_probability.substrates.jax.bijectors as tfb
import tensorflow_probability.substrates.jax.distributions as tfd
from jax import tree_map
from jax import vmap
from jax.tree_util import register_pytree_node_class
from ssm_jax.abstractions import Parameter
from ssm_jax.hmm.inference import compute_transition_probs
from ssm_jax.hmm.inference import hmm_smoother
from ssm_jax.hmm.models.base import BaseHMM


@register_pytree_node_class
class PoissonHMM(BaseHMM):

    def __init__(self, initial_probabilities, transition_matrix, emission_rates):
        """_summary_

        Args:
            initial_probabilities (_type_): _description_
            transition_matrix (_type_): _description_
            emission_rates (_type_): _description_
        """
        super().__init__(initial_probabilities, transition_matrix)
        self._emission_rates_param = Parameter(emission_rates, bijector=tfb.Invert(tfb.Softplus()))

    @classmethod
    def random_initialization(cls, key, num_states, emission_dim):
        key1, key2, key3 = jr.split(key, 3)
        initial_probs = jr.dirichlet(key1, jnp.ones(num_states))
        transition_matrix = jr.dirichlet(key2, jnp.ones(num_states), (num_states,))
        emission_rates = jr.exponential(key3, (num_states, emission_dim))
        return cls(initial_probs, transition_matrix, emission_rates)

    @property
    def emission_rates(self):
        return self._emission_rates_param.value

    def freeze_emission_rates(self):
        self._emission_rates_param.is_frozen = True

    def unfreeze_emission_rates(self):
        self._emission_rates_param.is_frozen = False

    # Properties to get various parameters of the model
    def emission_distribution(self, state):
        return tfd.Independent(tfd.Poisson(rate=self.emission_rates[state]), reinterpreted_batch_ndims=1)

    def e_step(self, batch_emissions):
        """The E-step computes expected sufficient statistics under the
        posterior. In the Gaussian case, this these are the first two
        moments of the data
        """

        @chex.dataclass
        class PoissonHMMSuffStats:
            # Wrapper for sufficient statistics of a BernoulliHMM
            marginal_loglik: chex.Scalar
            initial_probs: chex.Array
            trans_probs: chex.Array
            sum_w: chex.Array
            sum_x: chex.Array

        def _single_e_step(emissions):
            # Run the smoother
            posterior = hmm_smoother(self.initial_probabilities, self.transition_matrix,
                                     self._conditional_logliks(emissions))

            # Compute the initial state and transition probabilities
            initial_probs = posterior.smoothed_probs[0]
            trans_probs = compute_transition_probs(self.transition_matrix, posterior)

            # Compute the expected sufficient statistics
            sum_w = jnp.einsum("tk->k", posterior.smoothed_probs)[:, None]
            sum_x = jnp.einsum("tk, ti->ki", posterior.smoothed_probs, emissions)

            # Pack into a dataclass
            stats = PoissonHMMSuffStats(
                marginal_loglik=posterior.marginal_loglik,
                initial_probs=initial_probs,
                trans_probs=trans_probs,
                sum_w=sum_w,
                sum_x=sum_x,
            )
            return stats

        # Map the E step calculations over batches
        return vmap(_single_e_step)(batch_emissions)

    def m_step(self, batch_emissions, batch_posteriors, **kwargs):
        # Sum the statistics across all batches
        stats = tree_map(partial(jnp.sum, axis=0), batch_posteriors)
        # Then maximize the expected log probability as a fn of model parameters
        self._initial_probs_param.value = tfd.Dirichlet(1.0001 + stats.initial_probs).mode()
        self._transition_probs_param.value = tfd.Dirichlet(1.0001 + stats.trans_probs).mode()
        self._emission_rates_param.value = tfd.Gamma(1.1 + stats.sum_x, 1.1 + stats.sum_w).mode()
