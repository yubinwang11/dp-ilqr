#!/usr/bin/env python

"""Functional programming implementation of the iLQR solver.

[1] Jackson. AL iLQR Tutorial. https://bjack205.github.io/papers/AL_iLQR_Tutorial.pdf

"""


import torch


class iLQR:
    """
    iLQR solver with a functional approach
    """

    DELTA_0 = 2.0  # initial regularization scaling
    MU_MIN = 1e-6  # regularization floor, below which is 0.0
    MU_MAX = 1e3  # regularization ceiling
    N_LS_ITER = 10  # number of line search iterations

    def __init__(self, dynamics, cost, n_x, n_u, dt=0.1, N=10):

        self.dynamics = dynamics
        self.cost = cost

        self.N = N
        self.dt = dt
        self.n_x = n_x
        self.n_u = n_u

        self._reset_regularization()

    def _rollout(self, x0, U):
        """Rollout the system from an initial state with a control sequence U."""

        N = U.shape[0]
        X = torch.zeros((N + 1, self.n_x))
        X[0] = x0.flatten()
        J = 0.0

        for t in range(N):
            X[t + 1] = self.dynamics(X[t], U[t])
            J += self.cost(X[t], U[t]).item()
        J += self.cost(X[-1], torch.zeros(self.n_u), terminal=True).item()

        return X, J

    def _forward_pass(self, X, U, K, d, α):
        """Forward pass to rollout the control gains K and d."""

        X_next = torch.zeros((self.N + 1, self.n_x))
        U_next = torch.zeros((self.N, self.n_u))

        X_next[0] = X[0]
        J = 0.0

        for t in range(self.N):
            δx = X_next[t] - X[t]
            δu = K[t] @ δx + α * d[t]

            U_next[t] = U[t] + δu
            X_next[t + 1] = self.dynamics(X_next[t], U_next[t])

            J += self.cost(X_next[t], U_next[t]).item()
        J += self.cost(X_next[-1], torch.zeros((self.n_u)), terminal=True).item()

        return X_next, U_next, J

    def _backward_pass(self, X, U):
        """Backward pass to compute gain matrices K and d from the trajectory."""

        K = torch.zeros((self.N, self.n_u, self.n_x))  # linear feedback gain
        d = torch.zeros((self.N, self.n_u))  # feedforward gain

        # self.μ = 0.0 # DBG
        reg = self.μ * torch.eye(self.n_x)

        L_x, _, L_xx, _, _ = self.cost.quadraticize(
            X[-1], torch.zeros(self.n_u), terminal=True
        )
        p = L_x
        P = L_xx

        for t in range(self.N - 1, -1, -1):
            L_x, L_u, L_xx, L_uu, L_ux = self.cost.quadraticize(X[t], U[t])
            A, B = self.dynamics.linearize(X[t], U[t])

            Q_x = L_x + A.T @ p
            Q_u = L_u + B.T @ p
            Q_xx = L_xx + A.T @ P @ A
            Q_uu = L_uu + B.T @ (P + reg) @ B
            Q_ux = L_ux + B.T @ (P + reg) @ A

            K[t] = -torch.linalg.solve(Q_uu, Q_ux)
            d[t] = -torch.linalg.solve(Q_uu, Q_u)

            p = Q_x + K[t].T @ Q_uu @ d[t] + K[t].T @ Q_u + Q_ux.T @ d[t]
            P = Q_xx + K[t].T @ Q_uu @ K[t] + K[t].T @ Q_ux + Q_ux.T @ K[t]
            P = 0.5 * (P + P.T)

        return K, d

    def solve(self, x0, U=None, n_lqr_iter=50, tol=1e-3):

        if U is None:
            U = torch.zeros((self.N, self.n_u))
            # U = torch.full((self.N, self.n_u), 0.1)
            # U = 1e-3 * torch.rand((self.N, self.n_u))
        if U.shape != (self.N, self.n_u):
            raise ValueError

        self._reset_regularization()

        x0 = x0.reshape(-1, 1)
        is_converged = False
        alphas = 1.1 ** (-torch.arange(self.N_LS_ITER, dtype=torch.float32) ** 2)

        X, J_star = self._rollout(x0, U)

        print(f"0/{n_lqr_iter}\tJ: {J_star:g}")
        for i in range(n_lqr_iter):
            accept = False

            # Backward recurse to compute gain matrices.
            K, d = self._backward_pass(X, U)

            # Conduct a line search to find a satisfactory trajectory where we
            # continually decrease α. We're effectively getting closer to the
            # linear approximation in the LQR case.
            for α in alphas:

                X_next, U_next, J = self._forward_pass(X, U, K, d, α)

                if J < J_star:
                    if abs((J_star - J) / J_star) < tol:
                        is_converged = True

                    X = X_next
                    U = U_next
                    J_star = J
                    self._decrease_regularization()

                    accept = True
                    break

            if not accept:

                # Increase regularization if we're not converging.
                print("Failed line search.. increasing μ.")
                self._increase_regularization()
                if self.μ >= self.MU_MAX:
                    print("Exceeded max regularization term...")
                    break

            if is_converged:
                break

            print(f"{i+1}/{n_lqr_iter}\tJ: {J_star:g}\tμ: {self.μ:g}\tΔ: {self.Δ:g}")
        
        return X.detach(), U.detach(), J

    def _reset_regularization(self):
        """Reset regularization terms to their factory defaults."""
        self.μ = 1.0  # regularization
        self.Δ = self.DELTA_0  # regularization scaling

    def _decrease_regularization(self):
        """Decrease regularization to converge more slowly."""
        self.Δ = min(1.0, self.Δ) / self.DELTA_0
        self.μ *= self.Δ
        if self.μ <= self.MU_MIN:
            self.μ = 0.0

    def _increase_regularization(self):
        """Increase regularization to go a different direction"""
        self.Δ = max(1.0, self.Δ) * self.DELTA_0
        self.μ = max(self.MU_MIN, self.μ * self.Δ)

    def __repr__(self):
        return (
            f"iLQR(\n\tdynamics: {self.dynamics},\n\tcost: {self.cost},"
            f"\n\tN: {self.N},\n\tdt: {self.dt},\n\tμ: {self.μ},\n\tΔ: {self.Δ}"
            "\n)"
        )


# Based off of: https://github.com/anassinator/ilqr/blob/master/ilqr/controller.py
class RecedingHorizonController:
    """Receding horizon controller

    Attributes
    ----------
    _x : np.ndarray
        Current state
    _controller : BaseController
        Controller instance initialized with all necessary costs
    step_size : int, default=1
        Number of steps to take between controller fits

    """

    def __init__(self, x0, controller, step_size=1):
        self.x = x0
        self._controller = controller
        self.step_size = step_size

    @property
    def N(self):
        return self._controller.N

    def solve(self, U_init, J_converge=1.0, **kwargs):
        """Optimize the system controls from the current state

        Parameters
        ----------
        U_init : np.ndarray
            Initial inputs to provide to the controller
        J_converge : float
            Cost defining convergence to the goal, which causes us to stop if 
            reached
        **kwargs
            Additional keyword arguments to pass onto the ``controller.solve``.

        Returns
        -------
        X : np.ndarray
            Resulting trajectory computed by controller of shape (step_size, n_x)
        U : np.ndarray
            Control sequence applied of shape (step_size, n_u)
        J : float
            Converged cost value

        """

        i = 0
        U = U_init
        while True:
            print("-" * 40 + f"\nHorizon {i}")
            i += 1
            
            # Fit the current state initializing with our control sequence.
            if U.shape != (self._controller.N, self._controller.n_u):
                raise RuntimeError

            X, U, J = self._controller.solve(self.x, U, **kwargs)

            # Add random noise to the trajectory to add some realism.
            # X += torch.random.normal(size=X.shape, scale=0.05)

            # Shift the state to our predicted value. NOTE: this can be
            # updated externally for actual sensor feedback.
            self.x = X[self.step_size]

            yield X[: self.step_size], U[: self.step_size], J

            U = U[self.step_size :]
            U = torch.vstack([
                U, 
                torch.zeros((self.step_size, self._controller.n_u))
            ])

            if J < J_converge:
                print("Converged!")
                break
            

class NavigationProblem:

    """Splits a centralized optimal control problem into a decentralized one

    Attributes
    ----------
    planning_radius : float, default=10.0
        The splitting distance between agents. If two agents come within each 
        others' planning radii, we split the centralized problem into distinct
        sub-problems.

    """

    def __init__(self, planning_radius=10.0):
        self.planning_radius = planning_radius

    def delegate_subproblems(self, multi_dynamics, game_cost):
        raise NotImplementedError
