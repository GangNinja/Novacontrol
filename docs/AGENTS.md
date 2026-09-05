# Multi-Agent System

The Phase 5 agent subsystem provides deterministic routing and agent communication contracts. It is ready for LLM-backed implementations in later phases without changing the public boundary.

## Agents

Default specialized agents:

- Research Agent
- Coding Agent
- Debug Agent
- Planning Agent
- Documentation Agent
- Design Agent
- Testing Agent
- Review Agent
- Browser Agent
- Desktop Automation Agent
- Project Manager Agent

The Coordinator Agent routes tasks by intent and delegates them through the `AgentRegistry`.

## Components

- `AgentTask`: goal, role, metadata, status, and id
- `AgentResponse`: structured task result
- `AgentMessage`: agent-to-agent message
- `AgentMessageBus`: in-process agent message history and delivery
- `AgentRegistry`: stores agents by name and role
- `CoordinatorAgent`: chooses the best role for a task
- `AgentModule`: event-driven runtime module for task delegation

## Events

- `agent.task_requested`: asks the coordinator to route and execute a task
- `agent.task_completed`: emitted when an agent completes a task
- `agent.task_failed`: emitted when delegation fails

## Future Work

Later phases should connect agents to memory, tools, planning graphs, LLM providers, and progress tracking persistence.
