"""Kubernetes API connector (stub)."""

from __future__ import annotations


class KubernetesConnector:
    def __init__(self, kubeconfig: str | None = None, namespace: str = "default") -> None:
        self.kubeconfig = kubeconfig
        self.namespace = namespace

    def get_pods(self, label_selector: str = "") -> list[dict]:
        raise NotImplementedError

    def get_resource_usage(self, pod: str) -> dict:
        raise NotImplementedError
