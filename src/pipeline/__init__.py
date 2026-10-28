"""Pipeline: stage orchestration and output generation."""

from src.pipeline.context import PipelineContext
from src.pipeline.stages import STAGES

__all__ = ["STAGES", "PipelineContext"]
