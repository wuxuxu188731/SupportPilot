from fastapi import FastAPI

from app.agent.prompts import SUPPORT_SYSTEM_PROMPT
from app.agent.support_runner import CustomerSupportAgentRunner
from app.actions.factory import build_action_executor
from app.actions.service import ActionWorkflowService
from app.actions.sqlite_store import SQLiteActionStore
from app.api.router import creat_conversation_router
from app.api.action_router import create_action_router
from app.api.auth_router import create_auth_router
from app.api.dependencies import create_current_user_dependency, create_current_tenant_dependency
from app.api.organization_router import create_organization_router
from app.application.chat_service import ChatService
from app.application.auth_service import AuthService,AccessTokenService
from app.application.organization_service import OrganizationService
from app.concurrency.conversation_locks import ConversationLockRegistry
from app.core.config import (
  MODEL_NAME,
  create_llm_client,
  get_chat_db_path,
  get_auth_secret_key,
  get_access_token_ttl_seconds,
  get_knowledge_settings,
  get_checkpoint_db_path
)
from app.core.cors import configure_cors
from app.api.knowledge_router import create_knowledge_router
from app.knowledge.factory import create_knowledge_services
from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.orders.sqlite_store import SQLiteOrderStore
from app.sessions.sqlite_store import SQLiteSessionStore
from app.users.sqlite_store import SQLiteUserStore
from app.tools.support_factory import create_customer_support_tool_gateway
from app.tools.action_gateway import ActionToolGateway
from app.tools.knowledge_gateway import KnowledgeToolGateway
from app.tools.composite_gateway import CompositeToolGateway
from app.workflows.action_graph import build_action_graph
from app.workflows.checkpointer import create_sqlite_checkpointer
from app.workflows.runtime import LangGraphActionWorkflowRunner



app = FastAPI()
# 装配 CORS：允许来源由 CORS_ALLOW_ORIGINS 配置（默认放行本机 Vite 开发
# 服务器 5173）。前端开发时走 /api 代理属于同源请求，此配置用于直连后端
# 或前后端分离部署的场景。
configure_cors(app)
client = create_llm_client()

database_path = get_chat_db_path()

session_store = SQLiteSessionStore(database_path=database_path)
user_store = SQLiteUserStore(database_path=database_path)

organization_store = SQLiteOrganizationStore(database_path=database_path)
organization_service = OrganizationService(
    organization_store=organization_store,
    user_store=user_store,
)

# 退款/补偿审批工作流装配：业务 Store -> 幂等执行器 -> LangGraph 图 ->
# 运行器 -> 应用服务。checkpoint 使用独立 SQLite 文件（设计 11.4）。
action_store = SQLiteActionStore(database_path=database_path)
action_executor = build_action_executor(action_store)
action_checkpointer = create_sqlite_checkpointer(get_checkpoint_db_path())
action_graph = build_action_graph(
    store=action_store,
    executor=action_executor,
    checkpointer=action_checkpointer,
)
action_runner = LangGraphActionWorkflowRunner(
    store=action_store,
    graph=action_graph,
)
action_service = ActionWorkflowService(
    store=action_store,
    organization_service=organization_service,
    runner=action_runner,
)

support_tool_gateway = create_customer_support_tool_gateway(
  database_path,
)
token_service = AccessTokenService(
  secret_key=get_auth_secret_key(),
  ttl_seconds=get_access_token_ttl_seconds()
)
auth_service=AuthService(
  user_store=user_store,
  token_service=token_service
)
get_current_user = create_current_user_dependency(auth_service=auth_service)

get_current_tenant = create_current_tenant_dependency(
    organization_service=organization_service,
    get_current_user=get_current_user,
)

# NOTE (I4 / Stage A): `knowledge_services.baseline` (the Baseline retriever) is
# deliberately NOT exposed over HTTP in Stage A. Only ingestion + store are wired
# into create_knowledge_router below. Stage B registers the baseline retriever as
# an agent tool.
knowledge_services = create_knowledge_services(
    database_path=database_path,
    settings=get_knowledge_settings(),
    llm_client=client,
    model_name=MODEL_NAME,
)
knowledge_tool_gateway = KnowledgeToolGateway(
  service=knowledge_services.adaptive,
)
# 动作工具 Gateway（设计 12）：只暴露 propose_refund / propose_compensation /
# get_action_status；审批、恢复与执行不进入工具面，只能通过认证 HTTP API。
action_tool_gateway = ActionToolGateway(
  service=action_service,
  order_store=SQLiteOrderStore(database_path),
)
composite_tool_gateway = CompositeToolGateway([
  support_tool_gateway,
  knowledge_tool_gateway,
  action_tool_gateway,
])
support_agent_runner = CustomerSupportAgentRunner(
  client=client,
  gateway=composite_tool_gateway,
  model_name=MODEL_NAME,
)
chat_service = ChatService(
  store=session_store,
  run_agent=support_agent_runner,
  locks=ConversationLockRegistry(),
  base_system_prompt=SUPPORT_SYSTEM_PROMPT,
)

app.include_router(
  create_knowledge_router(
    ingestion_service=knowledge_services.ingestion,
    knowledge_store=knowledge_services.store,
    get_current_tenant=get_current_tenant,
  )
)

app.include_router(
  creat_conversation_router(
    chat_service=chat_service,
    get_current_tenant=get_current_tenant,
  )
)
app.include_router(
  create_auth_router(
    auth_service=auth_service,
    get_current_user=get_current_user
  )
)
app.include_router(
  create_organization_router(
    organization_service=organization_service,
    get_current_user=get_current_user,
  )
)
app.include_router(
  create_action_router(
    action_service=action_service,
    get_current_tenant=get_current_tenant,
  )
)
