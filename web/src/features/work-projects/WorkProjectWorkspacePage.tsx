import { Button, Empty, Spin, TabPane, Tabs } from "@douyinfe/semi-ui";
import { ArrowLeft, Boxes, Bug, FileText, Network, Route } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { showApiError } from "../../shared/api/feedback";
import type { WorkProject } from "../../shared/api/types";
import { MetricStrip } from "../../shared/components/ResourcePageShell";
import { AssetList, AttackPathList, FindingList, GraphView } from "./ProjectRecordViews";
import {
  EMPTY_WORK_PROJECT_RECORDS,
  loadWorkProjectRecordSnapshot,
  type WorkProjectRecords,
} from "./workProjectRecords";
import { workProjectOwnerNames, WorkProjectStatusTag, WorkProjectTypeTag } from "./workProjectView";

export function WorkProjectWorkspacePage() {
  const params = useParams();
  const navigate = useNavigate();
  const projectId = Number(params.projectId);
  const [project, setProject] = useState<WorkProject | null>(null);
  const [records, setRecords] = useState<WorkProjectRecords>(EMPTY_WORK_PROJECT_RECORDS);
  const [loading, setLoading] = useState(false);

  const loadWorkspace = useCallback(async () => {
    if (!Number.isFinite(projectId) || projectId <= 0) return;
    setLoading(true);
    try {
      const snapshot = await loadWorkProjectRecordSnapshot(projectId);
      setProject(snapshot.project);
      setRecords(snapshot.records);
    } catch (error) {
      showApiError(error);
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void loadWorkspace();
  }, [loadWorkspace]);

  const metrics = useMemo(() => [
    { label: "Assets", value: records.assets.length },
    { label: "Findings", value: records.findings.length },
    { label: "Relationships", value: records.graph.edges.length },
    { label: "Sessions", value: project?.session_count ?? 0 },
  ], [project, records]);

  if (!Number.isFinite(projectId) || projectId <= 0) {
    return <Empty className="empty-state" image={<FileText size={42} />} title="Invalid project" description="" />;
  }

  return (
    <section className="work-project-workspace">
      <div className="workspace-back-row">
        <Button icon={<ArrowLeft size={15} />} theme="borderless" onClick={() => navigate("/work-projects")}>
          Back
        </Button>
      </div>
      <div className="workspace-header">
        {project ? (
          <div className="workspace-title">
            <div className="workspace-title-main">
              <h2>{project.name}</h2>
              {project.description ? <p>{project.description}</p> : null}
              <span>Owners: {workProjectOwnerNames(project)}</span>
            </div>
            <div className="workspace-title-tags">
              <WorkProjectTypeTag project={project} />
              <WorkProjectStatusTag project={project} />
            </div>
          </div>
        ) : null}
      </div>

      <MetricStrip metrics={metrics} />

      <Spin spinning={loading}>
        <Tabs className="workspace-tabs" type="line">
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
      </Spin>
    </section>
  );
}

function TabLabel({ icon, text }: { icon: ReactNode; text: string }) {
  return <span className="workspace-tab-label">{icon}{text}</span>;
}
