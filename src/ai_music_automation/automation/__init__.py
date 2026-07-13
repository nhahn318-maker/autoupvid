"""Reusable automation primitives for niche content pipelines."""

from .artifacts import (
    AutomationArtifact,
    ImageArtifact,
    MetadataArtifact,
    PipelineArtifacts,
    SceneArtifact,
    StoryArtifact,
    VoiceArtifact,
)
from .cache import AutomationCache
from .logging import AutomationLogger
from .model_client import ModelRequest, OllamaClient
from .niche import NicheProfile, sleep_story_profile

__all__ = [
    "AutomationArtifact",
    "AutomationCache",
    "AutomationLogger",
    "ImageArtifact",
    "MetadataArtifact",
    "ModelRequest",
    "NicheProfile",
    "OllamaClient",
    "PipelineArtifacts",
    "sleep_story_profile",
    "SceneArtifact",
    "StoryArtifact",
    "VoiceArtifact",
]
