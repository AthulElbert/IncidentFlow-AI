import logging
from datetime import datetime, timezone
from uuid import uuid4

from app.adapters.jenkins_client import JenkinsClient, MockJenkinsClient
from app.adapters.jira_client import JiraClient, MockJiraClient
from app.models.schemas import APMEvent, IncidentRecord, IncidentResponse
from app.services.change_control import ChangeControlStore
from app.services.fix_planner import build_ticket_text, suggest_runbook_actions
from app.services.knowledge_base import IncidentKnowledgeBase
from app.services.pattern_detector import PatternDetector
from app.services.triage_agent import TriageAgent

logger = logging.getLogger("app.pipeline")


class SupportAgentPipeline:
    def __init__(
        self,
        kb_file: str,
        jira_client: JiraClient,
        jenkins_client: JenkinsClient,
        jira_mode: str,
        jenkins_mode: str,
        change_store: ChangeControlStore,
        triage_agent: TriageAgent,
        storage_backend: str = "json",
        database_url: str = "",
    ) -> None:
        self.jira = jira_client
        self.jenkins = jenkins_client
        self.jira_mode = jira_mode
        self.jenkins_mode = jenkins_mode
        self.change_store = change_store
        self.triage_agent = triage_agent
        self.patterns = PatternDetector()
        self.kb = IncidentKnowledgeBase(
            kb_file,
            storage_backend=storage_backend,
            database_url=database_url,
        )
        self._jira_mock_fallback = MockJiraClient(project_key="SUP")
        self._jenkins_mock_fallback = MockJenkinsClient()

    def process_event(self, event: APMEvent) -> IncidentResponse:
        logger.info(
            "incident-received",
            extra={
                "service": event.service,
                "metric": event.metric,
                "environment": event.environment,
            },
        )

        triage = self.triage_agent.triage(event)
        issue_type = triage.issue_type
        confidence = triage.confidence
        probable_cause = triage.probable_cause

        is_recurring, recurrence_count = self.patterns.detect_recurrence(event)

        similar = self.kb.find_similar(
            service=event.service,
            metric=event.metric,
            issue_type=issue_type,
        )
        suggestions = suggest_runbook_actions(issue_type, event)

        summary, description = build_ticket_text(
            event=event,
            issue_type=issue_type,
            probable_cause=probable_cause,
            is_recurring=is_recurring,
        )
        labels = [event.service, event.environment, issue_type, "agent-generated"]

        warnings: list[str] = list(triage.warnings)
        jira_mode_used = self.jira_mode
        jenkins_mode_used = self.jenkins_mode

        try:
            jira_ticket = self.jira.create_ticket(summary=summary, description=description, labels=labels)
        except Exception as exc:
            warning = f"Jira integration failed; used mock fallback: {exc}"
            warnings.append(warning)
            logger.warning("jira-fallback", extra={"error": str(exc)})
            jira_ticket = self._jira_mock_fallback.create_ticket(
                summary=summary,
                description=description,
                labels=labels,
            )
            jira_mode_used = "mock-fallback"

        try:
            jenkins_result = self.jenkins.trigger_dev_validation(
                service=event.service,
                issue_type=issue_type,
            )
        except Exception as exc:
            warning = f"Jenkins integration failed; used mock fallback: {exc}"
            warnings.append(warning)
            logger.warning("jenkins-fallback", extra={"error": str(exc)})
            jenkins_result = self._jenkins_mock_fallback.trigger_dev_validation(
                service=event.service,
                issue_type=issue_type,
            )
            jenkins_mode_used = "mock-fallback"

        incident = IncidentRecord(
            incident_id=f"INC-{str(uuid4()).split('-')[0].upper()}",
            service=event.service,
            metric=event.metric,
            issue_type=issue_type,
            summary=summary,
            resolution=(
                similar[0].resolution
                if similar
                else "No known resolution. Apply suggested runbook actions in dev and validate."
            ),
            runbook_actions=suggestions,
            created_at=datetime.now(timezone.utc),
        )
        self.kb.add_record(incident)

        change = self.change_store.create_change(
            incident_id=incident.incident_id,
            service=event.service,
            environment=event.environment,
            summary=summary,
            jira_key=jira_ticket.key,
            jenkins_job_url=jenkins_result.url,
            proposed_actions=suggestions,
            triage_mode_used=triage.mode_used,
            triage_hypothesis_steps=triage.hypothesis_steps,
            issue_type=issue_type,
            confidence=confidence,
            warning_count=len(warnings),
            jenkins_status=jenkins_result.status,
        )

        logger.info(
            "incident-processed",
            extra={
                "issue_type": issue_type,
                "confidence": confidence,
                "is_recurring": is_recurring,
                "jira_mode": jira_mode_used,
                "jenkins_mode": jenkins_mode_used,
                "change_id": change.change_id,
                "change_status": change.status,
                "triage_mode": triage.mode_used,
            },
        )

        return IncidentResponse(
            issue_type=issue_type,
            confidence=confidence,
            is_recurring=is_recurring,
            recurrence_count=recurrence_count,
            probable_cause=probable_cause,
            suggested_actions=suggestions,
            similar_incidents=similar,
            jira_ticket=jira_ticket,
            jenkins_validation=jenkins_result,
            metadata={
                "environment": event.environment,
                "ingested_at": datetime.now(timezone.utc).isoformat(),
                "jira_mode": jira_mode_used,
                "jenkins_mode": jenkins_mode_used,
                "triage_mode": triage.mode_used,
                "triage_hypothesis_steps": triage.hypothesis_steps,
                "warnings": warnings,
                "change_id": change.change_id,
                "change_status": change.status,
                "deployment_state": change.deployment_state,
                "approval_required": True,
                "policy_reasons": change.policy_reasons,
            },
        )
