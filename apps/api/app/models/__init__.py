"""Import all model modules so SQLAlchemy resolves every relationship."""
from app.db.base import Base  # noqa: F401
from .base import TimestampMixin  # noqa: F401
from .user import User  # noqa: F401
from .refresh_token import RefreshToken  # noqa: F401
from .company import Company, Department  # noqa: F401
from .membership import CompanyMember, DepartmentMember  # noqa: F401
from .member_invite import MemberInvite  # noqa: F401
from .agent import Agent, AgentModelConfig, Model, ModelType  # noqa: F401
from .model_provider import ModelProvider  # noqa: F401
from .skill import AgentSkill, Skill  # noqa: F401
from .task import (AgentExecution, ExecutionCheckpoint, Intervention, Task,  # noqa: F401
                   TaskEvent)
from .message import Conversation, Message  # noqa: F401
from .asset import Asset  # noqa: F401
from .event import EventLog  # noqa: F401
from .project import (Milestone, Project, ProjectContextSnapshot,  # noqa: F401
                      ProjectMember)
from .workflow import (WorkflowDefinition, WorkflowEdge, WorkflowExecution,  # noqa: F401
                       WorkflowNode, WorkflowStep)
from .meeting import (Meeting, MeetingActionItem, MeetingDecision,  # noqa: F401
                      MeetingMessage, MeetingParticipant)
from .collaboration import (AgentCollaboration, AgentMessage,  # noqa: F401
                            CollaborationMessageType, CollaborationType)
from .knowledge import KBChunk, KBDocument, KBDocumentStatus  # noqa: F401
from .memory import AgentMemory, MemoryType  # noqa: F401
from .artifact import Artifact, ArtifactStatus, ArtifactVersion  # noqa: F401
from .deliverable import DeliverableComment  # noqa: F401
from .queue_item import QueueItem, QueueItemStatus  # noqa: F401
from .subscription import (CompanySubscription, SubscriptionOrder,  # noqa: F401
                           SubscriptionOrderStatus, SubscriptionStatus)
from .company_secret import CompanySecret  # noqa: F401
from .intervention import (ApprovalRequest, InterventionPolicy,  # noqa: F401
                           OverrideLog)
from .performance import PerformanceSnapshot  # noqa: F401
from .device import Device, PairingToken  # noqa: F401
from app.browser_agent import BrowserActionLog  # noqa: F401
from app.computer_agent import ComputerActionLog, ComputerPermission, KNOWN_CAPABILITIES  # noqa: F401
from .app_alias import AppAliasRule  # noqa: F401
from .scheduler import AutomationRun, AutomationSchedule  # noqa: F401
from .learning import (AgentCapabilityHistory, AgentGrowthEvent,  # noqa: F401
                       AgentLearningRecord, AgentProfile)
from .training import (TrainingApplication, TrainingLearningPoint,  # noqa: F401
                       TrainingProposal, TrainingSession)
from .reflection import (AgentExperience, AgentReflection,  # noqa: F401
                         ExperienceLesson, ImprovementProposal)
from .skill_growth import (AgentSkillNode, SkillEvaluation, SkillLevel,  # noqa: F401
                           SkillNode)
from .career import (AgentCareerRecord, CareerPosition,  # noqa: F401
                     CareerProposal, TrialAssignment)
from .token_usage import PricingConfig, TokenUsage  # noqa: F401
from .expert_template import ExpertTemplate  # noqa: F401
from .review import AgentReview, ReviewCycle  # noqa: F401
from app.factory_engine import AgentFactoryJob  # noqa: F401
from .war_room import WarRoomSession, WarRoomTask  # noqa: F401
from app.external_agents import ExternalAgentConnection  # noqa: F401
from app.computer_workspace import (ComputerSession, ComputerWorkspace,  # noqa: F401
                                 SessionAction, WorkspaceFile)
from .automation import AutomationDefinition, AutomationRunRecord  # noqa: F401
from app.models.p3_governance import (AgentSandbox, AgentVersion,  # noqa: F401
                                      AutonomousExecutionLog,
                                      AutonomousSettings, CompanyCreationJob,
                                      EnterpriseTemplate, MarketplacePackage,
                                      PackageInstallation,
                                      SelfImprovementCase)
from .budget import TokenBudget  # noqa: F401
from .mailbox import AgentMailbox, MailMessage  # noqa: F401
from .chat_group import ChatGroup  # noqa: F401
from .agent_group import AgentGroup  # noqa: F401
from .whitelist import ApprovalWhitelist  # noqa: F401
from .worklog import AgentWorklog, WorklogStatus  # noqa: F401
from .ceo import CeoMission  # noqa: F401
from .ceo_thread import CeoThread  # noqa: F401
from .evolution import CeoPlaybook, EvolutionRecord  # noqa: F401
from .exam import CeoExam  # noqa: F401
from .experience import SharedExperience  # noqa: F401
from .notification import Notification  # noqa: F401
from .push_subscription import PushSubscription  # noqa: F401
from .thread_read import ThreadRead  # noqa: F401
from .plugin import ComputerPolicy, PluginInstallation  # noqa: F401
from .org_synthesis import OrgSynthesisRun, OrgSynthesisStatus  # noqa: F401
from .computer_device import (ComputerDevice, ComputerDispatch,  # noqa: F401
                              DISPATCH_STATUSES, ONLINE_WINDOW)
