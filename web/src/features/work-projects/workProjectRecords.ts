import {
  getWorkProject,
  getWorkProjectGraph,
  queryWorkProjectFindings,
} from "../../shared/api/workProjects";
import type {
  WorkProject,
  WorkProjectAsset,
  WorkProjectFinding,
  WorkProjectGraphSnapshot,
} from "../../shared/api/types";

export type ProjectRecordTab = "assets" | "findings" | "attack-paths" | "graph";

export type WorkProjectRecords = {
  assets: WorkProjectAsset[];
  findings: WorkProjectFinding[];
  graph: WorkProjectGraphSnapshot;
};

export type WorkProjectRecordSnapshot = {
  project: WorkProject | null;
  records: WorkProjectRecords;
};

export const EMPTY_WORK_PROJECT_GRAPH: WorkProjectGraphSnapshot = {
  edges: [],
  attack_paths: [],
  attack_path_steps: [],
};

export const EMPTY_WORK_PROJECT_RECORDS: WorkProjectRecords = {
  assets: [],
  findings: [],
  graph: EMPTY_WORK_PROJECT_GRAPH,
};

export async function loadWorkProjectRecordSnapshot(projectId: number): Promise<WorkProjectRecordSnapshot> {
  const [projectResponse, findingsResponse, graphResponse] = await Promise.all([
    getWorkProject(projectId),
    queryWorkProjectFindings(projectId, { page: 1, size: 100, keyword: "" }),
    getWorkProjectGraph(projectId),
  ]);
  const project = projectResponse.data ?? null;

  return {
    project,
    records: {
      assets: project?.assets ?? [],
      findings: findingsResponse.data?.items ?? [],
      graph: graphResponse.data ?? EMPTY_WORK_PROJECT_GRAPH,
    },
  };
}
