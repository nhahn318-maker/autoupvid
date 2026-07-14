from __future__ import annotations

from dataclasses import dataclass, field

from .base import AgentContext, BaseAgent
from .json_utils import as_str_list, extract_json_object
from ..automation.model_client import ModelRequest, OllamaClient


@dataclass(frozen=True)
class StoryPlan:
    title: str
    hook: str
    outline: list[str] = field(default_factory=list)
    ending: str = ""
    lesson: str = ""
    protagonist: str = ""
    emotional_burden: str = ""
    concrete_memory_object: str = ""
    magical_mystery: str = ""
    micro_journey: str = ""
    choice_action: str = ""
    final_sleep_image: str = ""


@dataclass(frozen=True)
class StoryPlannerInput:
    topic: str
    niche_prompt: str
    target_minutes: int = 3


class StoryPlannerAgent(BaseAgent[StoryPlannerInput, StoryPlan]):
    name = "story_planner"

    def __init__(self, model_client: OllamaClient | None = None, max_retries: int = 0) -> None:
        super().__init__(max_retries=max_retries)
        self.model_client = model_client or OllamaClient()

    def execute(self, payload: StoryPlannerInput, context: AgentContext) -> StoryPlan:
        response = self.model_client.generate(
            ModelRequest(
                base_url=str(context.settings.get("ollama_url") or "http://127.0.0.1:11434"),
                model=str(context.settings.get("ollama_model") or context.settings.get("model") or "gemma4:e2b"),
                prompt=build_plan_prompt(payload, context.niche),
                temperature=float(context.settings.get("planner_temperature") or 0.65),
                top_p=float(context.settings.get("planner_top_p") or 0.9),
                timeout_seconds=int(context.settings.get("timeout_seconds") or 240),
            )
        )
        data = extract_json_object(response) or {}
        outline = as_str_list(data.get("outline"))
        if not outline:
            outline = fallback_outline(payload.topic)
        plan = StoryPlan(
            title=str(data.get("title") or payload.topic).strip()[:100],
            hook=str(data.get("hook") or outline[0]).strip(),
            outline=outline,
            ending=str(data.get("ending") or "The listener is left with a peaceful image and a slower breath.").strip(),
            lesson=str(data.get("lesson") or "It is safe to soften and let the day go.").strip(),
            protagonist=str(data.get("protagonist") or "").strip(),
            emotional_burden=str(data.get("specific_emotional_burden") or data.get("emotional_burden") or "").strip(),
            concrete_memory_object=str(data.get("concrete_memory_object") or "").strip(),
            magical_mystery=str(data.get("small_magical_mystery") or data.get("magical_mystery") or "").strip(),
            micro_journey=str(data.get("micro_journey") or "").strip(),
            choice_action=str(data.get("choice_action") or "").strip(),
            final_sleep_image=str(data.get("final_sleep_image") or "").strip(),
        )
        return validate_story_plan(plan, payload.topic)


def build_plan_prompt(payload: StoryPlannerInput, niche: str) -> str:
    return f"""Plan a story for niche: {niche}.

Topic: {payload.topic}
Target minutes: {payload.target_minutes}
Niche direction:
{payload.niche_prompt}

This must be a real bedtime story with a memorable emotional point, not a guided meditation
and not only a scenic description. Build a clear gentle narrative arc:
- a named main character with one specific inner flaw or burden
- an adult emotional wound or tired feeling the listener can recognize
- a specific premise anchor: protagonist, emotional burden, concrete memory/object,
  small magical mystery, micro-journey, choice/action, and final sleep image
- a gentle fantasy-comic premise with one visually memorable magical rule
- a peaceful but concrete setting
- a small wish, question, secret, promise, or emotional need
- one concrete memory behind the burden, such as an unsent letter, a promise someone did not keep,
  a chair left waiting by a window, a cup saved for someone, or an apology never spoken
- one symbolic object that can appear in multiple scenes and carry the emotional meaning
- a small magical mystery around that object, discovery, or memory
- 8-10 visually different set pieces for videos 20 minutes or longer, each with a clear location,
  action, symbolic object, and visual change while sharing one art style
- a retention structure similar to: description -> discovery -> small mystery -> discovery -> memory ->
  new room/place -> new object -> another revelation -> kind choice -> sleep resolution
- a visible retention beat every 2-4 minutes: each beat should change the listener's question, the place,
  the object state, or the character's understanding without adding danger or urgency
- one gentle choice where the character gives up fear, pride, hurry, control, or loneliness
- a clear life lesson that arrives through action, not a lecture
- a calm resolution that naturally leads into sleep

Keep it safe for sleep: no villains, no danger, no panic, no loud conflict, no urgent stakes.
The story can have a very small problem, such as feeling lost, missing home, being unable to sleep,
or wondering where a light came from, but it must resolve softly.

Make it a little more wondrous than ordinary bedtime scenery. Prefer calm fairytale/comic ideas like:
- a rain library whose books open only when someone forgives a small regret
- a bridge that remembers the footsteps of people who learned to rest
- a cloud bakery that bakes dreams from unsent letters
- a tide bell that rings only when someone stops trying to carry everything alone
- a mushroom village, glass garden, moon railway, sleepy constellation shop, or lantern map

Do not repeat the same old structure every time. Avoid defaulting to a clockmaker, teacup, lost lantern,
moon meadow, huge moon, or generic cottage unless the topic specifically requires it.

The story should have one quiet "why it matters" idea. Examples:
- a tired adult learns that an old promise can be released without becoming bitter
- an old keeper learns that rest is also a way of caring
- a lonely traveler learns that home can begin with one kind promise
- a hurried apprentice learns that gentle work lasts longer than perfect work

Return only JSON:
{{
  "title": "final title",
  "hook": "one opening sentence, 18-32 words, speaking directly to a tired adult listener with If you have ever..., Have you ever..., or If you have been carrying...",
  "protagonist": "named adult protagonist with a simple visual identity",
  "specific_emotional_burden": "one concrete adult burden, not an abstract mood",
  "concrete_memory_object": "one drawable memory/person/object/promise behind the burden",
  "small_magical_mystery": "one calm curiosity hook tied to the object or setting",
  "micro_journey": "room/place -> threshold -> discovery -> choice -> return/rest",
  "choice_action": "one visible action that shows the lesson",
  "final_sleep_image": "the final drawable image before the adult sleep sign-off",
  "outline": [
    "beat 1 DESCRIPTION: SET PIECE, ACTION, OBJECT, LISTENER QUESTION - open with an adult emotional promise, then introduce the named character and the specific quiet hurt",
    "beat 2 DISCOVERY: SET PIECE, ACTION, OBJECT, LISTENER QUESTION - the first magical invitation opens a visually different location",
    "beat 3 SMALL MYSTERY: SET PIECE, ACTION, OBJECT, LISTENER QUESTION - a calm question appears and gives the listener a reason to continue",
    "beat 4 DISCOVERY: SET PIECE, ACTION, OBJECT, LISTENER QUESTION - the character follows the clue into a new visual place or state",
    "beat 5 MEMORY: SET PIECE, ACTION, OBJECT, LISTENER QUESTION - reveal one concrete memory behind the feeling through visible action",
    "beat 6 NEW ROOM: SET PIECE, ACTION, OBJECT, LISTENER QUESTION - enter a new chamber, garden, balcony, railway car, market, shore, archive, or other drawable location",
    "beat 7 NEW OBJECT: SET PIECE, ACTION, OBJECT, LISTENER QUESTION - introduce or transform one symbolic object that changes the meaning of the journey",
    "beat 8 ANOTHER REVELATION: SET PIECE, ACTION, OBJECT, LISTENER QUESTION - reveal a second quiet truth without making the story dramatic",
    "beat 9 KIND CHOICE: SET PIECE, ACTION, OBJECT, LISTENER QUESTION - the character makes one irreversible gentle choice shown through action",
    "beat 10 SLEEP RESOLUTION: SET PIECE, ACTION, OBJECT, LISTENER QUESTION - show what changed, then resolve with safety, rest, and sleep"
  ],
  "ending": "soft ending",
  "lesson": "one specific gentle lesson, not generic, written as a takeaway from the plot"
}}
"""


def fallback_outline(topic: str) -> list[str]:
    return [
        f"Introduce a named character in a peaceful place connected to {topic}, with one specific quiet problem or promise.",
        "Let the character notice a small invitation or discovery that gently tests that problem.",
        "Follow the character through calm actions and one meaningful meeting or obstacle.",
        "Let the character make a kind choice and learn a clear lesson through the consequence of that choice.",
        "Show what changed, then resolve with home, safety, stillness, and sleep coming closer.",
    ]


def validate_story_plan(plan: StoryPlan, topic: str) -> StoryPlan:
    """Ensure the writer receives concrete story anchors, not only mood guidance."""
    protagonist = plan.protagonist or "a named adult protagonist"
    emotional_burden = plan.emotional_burden or "one specific tender burden connected to the topic"
    concrete_memory_object = plan.concrete_memory_object or "one drawable memory object, promise, or person"
    magical_mystery = plan.magical_mystery or "one small magical mystery tied to that object"
    micro_journey = plan.micro_journey or "a calm path from familiar room to threshold, discovery, choice, and rest"
    choice_action = plan.choice_action or "one small irreversible gesture that shows the lesson"
    final_sleep_image = plan.final_sleep_image or "the protagonist resting beside the changed object"
    outline = list(plan.outline or fallback_outline(topic))
    premise_line = (
        "PREMISE LOCK: "
        f"protagonist={protagonist}; burden={emotional_burden}; "
        f"memory/object={concrete_memory_object}; mystery={magical_mystery}; "
        f"journey={micro_journey}; choice={choice_action}; final image={final_sleep_image}."
    )
    if not any("PREMISE LOCK:" in item for item in outline):
        outline.insert(0, premise_line)
    return StoryPlan(
        title=plan.title,
        hook=plan.hook,
        outline=outline,
        ending=plan.ending,
        lesson=plan.lesson,
        protagonist=protagonist,
        emotional_burden=emotional_burden,
        concrete_memory_object=concrete_memory_object,
        magical_mystery=magical_mystery,
        micro_journey=micro_journey,
        choice_action=choice_action,
        final_sleep_image=final_sleep_image,
    )
