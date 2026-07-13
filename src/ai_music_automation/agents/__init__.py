"""Agent interfaces for niche automation pipelines."""

from .base import AgentContext, AgentResult, BaseAgent
from .emotion_analyzer import EmotionAnalyzerAgent, EmotionSegment
from .image_reviewer import ImageReviewerAgent, ImageReviewInput
from .metadata_generator import MetadataGeneratorAgent, MetadataGeneratorInput
from .prompt_optimizer import PromptOptimizerAgent, PromptOptimizerInput
from .publisher_agent import PublishInput, PublishResult, PublisherAgent
from .qa_agent import QAAgent, QAInput, QAResult
from .render_agent import RenderAgent, RenderAgentInput
from .scene_planner import ScenePlannerAgent, ScenePlannerInput
from .scheduler_agent import SchedulerAgent, SchedulerInput
from .story_planner import StoryPlan, StoryPlannerAgent, StoryPlannerInput
from .story_reviewer import StoryReview, StoryReviewerAgent
from .story_writer import StoryWriterAgent, StoryWriterInput
from .thumbnail_generator import ThumbnailGeneratorAgent, ThumbnailPromptInput
from .topic_generator import TopicGeneratorAgent, TopicGeneratorInput
from .voice_agent import VoiceAgent, VoiceAgentInput

__all__ = [
    "AgentContext",
    "AgentResult",
    "BaseAgent",
    "EmotionAnalyzerAgent",
    "EmotionSegment",
    "ImageReviewerAgent",
    "ImageReviewInput",
    "MetadataGeneratorAgent",
    "MetadataGeneratorInput",
    "PromptOptimizerAgent",
    "PromptOptimizerInput",
    "PublishInput",
    "PublishResult",
    "PublisherAgent",
    "QAAgent",
    "QAInput",
    "QAResult",
    "RenderAgent",
    "RenderAgentInput",
    "ScenePlannerAgent",
    "ScenePlannerInput",
    "SchedulerAgent",
    "SchedulerInput",
    "StoryPlan",
    "StoryPlannerAgent",
    "StoryPlannerInput",
    "StoryReview",
    "StoryReviewerAgent",
    "StoryWriterAgent",
    "StoryWriterInput",
    "ThumbnailGeneratorAgent",
    "ThumbnailPromptInput",
    "TopicGeneratorAgent",
    "TopicGeneratorInput",
    "VoiceAgent",
    "VoiceAgentInput",
]
