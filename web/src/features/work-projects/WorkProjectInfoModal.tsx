import { Modal, Progress, Spin, TabPane, Tabs } from "@douyinfe/semi-ui";
import { Boxes, Bug, FileText, Network, Route, UserRound } from "lucide-react";
import { ReactNode, useEffect, useState } from "react";
import { showApiError } from "../../shared/api/feedback";
import type { WorkProject } from "../../shared/api/types";
import { AssetList, AttackPathList, FindingList, GraphView } from "./ProjectRecordViews";
import {
  EMPTY_WORK_PROJECT_RECORDS,
  loadWorkProjectRecordSnapshot,
  type ProjectRecordTab,
  type WorkProjectRecords,
} from "./workProjectRecords";
import {
  WorkProjectPanel,
  WorkProjectStatusTag,
  WorkProjectSummaries,
  WorkProjectTasks,
  WorkProjectTypeTag,
  workProjectOwnerNames,
} from "./workProjectView";

type WorkProjectInfoModalProps = {
  open: boolean;
  loading?: boolean;
  project: WorkProject | null;
  projectId?: number | null;
  initialTab?: ProjectRecordTab;
  onClose: () => void;
};

export function WorkProjectInfoModal({ open, loading = false, project, projectId, initialTab = "assets", onClose }: WorkProjectInfoModalProps) {
  const resolvedProjectId = project?.id ?? projectId ?? null;
  const [currentProject, setCurrentProject] = useState<WorkProject | null>(project);
  const [records, setRecords] = useState<WorkProjectRecords>(EMPTY_WORK_PROJECT_RECORDS);
  const [recordsLoading, setRecordsLoading] = useState(false);
  const [activeTab, setActiveTab] = useState<ProjectRecordTab>(initialTab);

  useEffect(() => {
    if (project) setCurrentProject(project);
  }, [project]);

  useEffect(() => {
    if (open) setActiveTab(initialTab);
  }, [initialTab, open]);

  useEffect(() => {
    if (!open || !resolvedProjectId) return;
    let canceled = false;
    setRecordsLoading(true);
    loadWorkProjectRecordSnapshot(resolvedProjectId)
      .then((snapshot) => {
        if (canceled) return;
        setCurrentProject(snapshot.project ?? project ?? null);
        setRecords(snapshot.records);
      })
      .catch((error) => {
        if (!canceled) showApiError(error);
      })
      .finally(() => {
        if (!canceled) setRecordsLoading(false);
      });
    return () => {
      canceled = true;
    };
  }, [open, project, resolvedProjectId]);

  const modalLoading = loading || recordsLoading;

  return (
    <Modal
      visible={open}
      title={<ProjectInfoTitle project={currentProject} />}
      width="min(1440px, calc(100vw - 24px))"
      footer={null}
      onCancel={onClose}
    >
      <Spin spinning={modalLoading}>
        {currentProject ? (
          <div className="project-info-content project-record-content">
            <section className="project-info-main">
              <section className="project-info-meta">
                <div>
                  <span>Type</span>
                  <WorkProjectTypeTag project={currentProject} />
                </div>
                <div>
                  <span>Status</span>
                  <WorkProjectStatusTag project={currentProject} />
                </div>
                <div>
                  <span>Owners</span>
                  <strong>{workProjectOwnerNames(currentProject)}</strong>
                </div>
                <div>
                  <span>Sandbox</span>
                  <strong>{currentProject.sandbox_container_id ?? "-"}</strong>
                </div>
              </section>

              {currentProject.description ? <div className="project-info-description">{currentProject.description}</div> : null}

              <section className="project-info-progress">
                <span>Task Progress</span>
                <Progress percent={currentProject.progress} size="small" showInfo />
              </section>

              <WorkProjectPanel
                title="Tasks"
                icon={<FileText size={15} />}
                empty={!currentProject.tasks.length ? "No data." : ""}
                className="project-info-panel"
                emptyClassName="project-info-empty"
              >
                <WorkProjectTasks project={currentProject} className="project-info-scroll-list project-info-tasks" rowClassName="project-info-task-row" />
              </WorkProjectPanel>

              <WorkProjectPanel
                title="Agent Summaries"
                icon={<UserRound size={15} />}
                empty={!currentProject.agent_summaries.length ? "No data." : ""}
                className="project-info-panel project-info-summary-panel"
                emptyClassName="project-info-empty"
              >
                <WorkProjectSummaries
                  project={currentProject}
                  className="project-info-scroll-list project-info-summaries project-info-summary-scroll"
                  rowClassName="project-info-summary"
                  progressClassName="project-info-summary-task"
                  blockClassName="project-info-summary-block"
                />
              </WorkProjectPanel>
            </section>

            <section className="project-record-panel">
              <Tabs type="line" className="project-record-tabs" activeKey={activeTab} onChange={(key) => setActiveTab(key as ProjectRecordTab)}>
                <TabPane tab={<TabLabel icon={<Boxes size={14} />} text="Assets" />} itemKey="assets">
                  <AssetList assets={records.assets} />
                </TabPane>
                <TabPane tab={<TabLabel icon={<Bug size={14} />} text="Findings" />} itemKey="findings">
                  <FindingList findings={records.findings} assets={records.assets} />
                </TabPane>
                <TabPane tab={<TabLabel icon={<Route size={14} />} text="Attack Paths" />} itemKey="attack-paths">
                  <AttackPathList assets={records.assets} graph={records.graph} />
                </TabPane>
                <TabPane tab={<TabLabel icon={<Network size={14} />} text="Graph" />} itemKey="graph">
                  <GraphView assets={records.assets} graph={records.graph} />
                </TabPane>
              </Tabs>
            </section>
          </div>
        ) : null}
      </Spin>
    </Modal>
  );
}

function ProjectInfoTitle({ project }: { project: WorkProject | null }) {
  return (
    <div className="project-info-title">
      <strong>{project?.name ?? "Work Project"}</strong>
    </div>
  );
}

function TabLabel({ icon, text }: { icon: ReactNode; text: string }) {
  return <span className="workspace-tab-label">{icon}{text}</span>;
}
