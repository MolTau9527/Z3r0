import { Button, Input, InputNumber, Select, Spin, Tag, TextArea } from "@douyinfe/semi-ui";
import { FolderKanban, Plus, ScanSearch, Server, Trash2, UserRound } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  getWorkProjectAssetTypes,
  getWorkProjectTypes,
  isWorkProjectAssetType,
  isWorkProjectType,
  WORK_PROJECT_ASSET_TYPE,
} from "../../shared/api/contract";
import { showApiError } from "../../shared/api/feedback";
import { queryAvailableSandboxContainers } from "../../shared/api/sandboxContainers";
import { querySystemUsers } from "../../shared/api/systemUsers";
import type {
  CreateWorkProjectRequest,
  SandboxContainer,
  SystemUser,
  WorkProject,
  WorkProjectAssetRequest,
} from "../../shared/api/types";
import { ResourceModal } from "../../shared/components/ResourceModal";
import {
  SANDBOX_CONTAINER_STATUS_COLOR,
  SANDBOX_CONTAINER_STATUS_LABEL,
  SYSTEM_USER_ROLE_COLOR,
  SYSTEM_USER_ROLE_LABEL,
  WORK_PROJECT_ASSET_TYPE_LABEL,
  WORK_PROJECT_TYPE_LABEL,
} from "../../shared/lib/labels";

type WorkProjectFormModalProps = {
  open: boolean;
  saving: boolean;
  project?: WorkProject | null;
  onCancel: () => void;
  onSubmit: (payload: CreateWorkProjectRequest) => Promise<void>;
};

type SelectedOption = {
  value?: SystemUser["id"];
};

type AssetFormRow = WorkProjectAssetRequest & {
  existing_id?: number;
};

type WorkProjectFormValues = Omit<CreateWorkProjectRequest, "assets"> & {
  assets: AssetFormRow[];
};

const projectTypes = getWorkProjectTypes();
const assetTypes = getWorkProjectAssetTypes();

const EMPTY_ASSET: AssetFormRow = {
  type: assetTypes[0],
  path: "",
  host: "",
  port: null,
};

const EMPTY: WorkProjectFormValues = {
  name: "",
  description: "",
  owner_user_ids: [],
  sandbox_container_id: null,
  assets: [{ ...EMPTY_ASSET }],
  type: projectTypes[0],
};

export function WorkProjectFormModal({ open, saving, project, onCancel, onSubmit }: WorkProjectFormModalProps) {
  const [values, setValues] = useState<WorkProjectFormValues>(EMPTY);
  const [sandboxContainers, setSandboxContainers] = useState<SandboxContainer[]>([]);
  const [users, setUsers] = useState<SystemUser[]>([]);
  const [sandboxLoading, setSandboxLoading] = useState(false);
  const [usersLoading, setUsersLoading] = useState(false);
  const editing = Boolean(project);

  const loadSandboxContainers = useCallback(async () => {
    setSandboxLoading(true);
    try {
      const response = await queryAvailableSandboxContainers({ page: 1, size: 100, keyword: "" });
      setSandboxContainers(response.data?.items ?? []);
    } catch (error) {
      showApiError(error);
    } finally {
      setSandboxLoading(false);
    }
  }, []);

  const loadUsers = useCallback(async () => {
    setUsersLoading(true);
    try {
      const response = await querySystemUsers({ page: 1, size: 100, keyword: "" });
      setUsers(response.data?.items ?? []);
    } catch (error) {
      showApiError(error);
    } finally {
      setUsersLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    setValues(project ? {
      name: project.name,
      description: project.description,
      owner_user_ids: project.owner_user_ids,
      sandbox_container_id: project.sandbox_container_id ?? null,
      assets: project.assets.length ? project.assets.map(assetFromProject) : [{ ...EMPTY_ASSET }],
      type: project.type,
    } : { ...EMPTY, assets: [{ ...EMPTY_ASSET }] });
    void loadSandboxContainers();
    void loadUsers();
  }, [loadSandboxContainers, loadUsers, open, project]);

  const userOptionList = useMemo(() => users.map((user) => ({
    label: <UserOption user={user} />,
    value: user.id,
  })), [users]);

  const sandboxOptionList = useMemo(() => sandboxContainers.map((container) => ({
    label: <SandboxContainerOption container={container} />,
    value: container.id,
  })), [sandboxContainers]);
  const selectedSandbox = sandboxContainers.find((container) => container.id === values.sandbox_container_id);
  const canSubmit = Boolean(values.name.trim()) && values.assets.length > 0
    && values.assets.every(isAssetComplete);

  const updateAsset = (index: number, patch: Partial<AssetFormRow>) => {
    setValues((current) => ({
      ...current,
      assets: current.assets.map((asset, assetIndex) => (
        assetIndex === index ? { ...asset, ...patch } : asset
      )),
    }));
  };

  const removeAsset = (index: number) => {
    setValues((current) => ({
      ...current,
      assets: current.assets.filter((_, assetIndex) => assetIndex !== index),
    }));
  };

  const submit = () => onSubmit({
    ...values,
    name: values.name.trim(),
    description: values.description.trim(),
    assets: values.assets.map(normalizeAsset).filter(isAssetComplete),
  });

  return (
    <ResourceModal
      open={open}
      title={editing ? "Edit Work Project" : "Create Work Project"}
      saving={saving}
      submitLabel={editing ? "Save" : "Create"}
      submitDisabled={!canSubmit}
      width={980}
      onCancel={onCancel}
      onSubmit={submit}
    >
      <div className="project-form-grid">
        <label>
          <span>Name</span>
          <Input prefix={<FolderKanban size={16} />} value={values.name} maxLength={255} required
            onChange={(name) => setValues((v) => ({ ...v, name }))}
          />
        </label>
        <label>
          <span>Type</span>
          <Select prefix={<ScanSearch size={16} />} value={values.type}
            onChange={(type) => isWorkProjectType(type) && setValues((v) => ({ ...v, type }))}
            optionList={projectTypes.map((type) => ({ label: WORK_PROJECT_TYPE_LABEL[type], value: type }))}
          />
        </label>
        <label>
          <span>Owners</span>
          <Select
            prefix={<UserRound size={16} />}
            value={values.owner_user_ids}
            optionList={userOptionList}
            placeholder={usersLoading ? "Loading users" : "Select project owners"}
            emptyContent={usersLoading ? <Spin size="small" /> : "No users"}
            loading={usersLoading}
            multiple
            renderSelectedItem={(option: SelectedOption) => ({
              isRenderInTag: true,
              content: users.find((user) => user.id === option.value)?.username ?? String(option.value ?? ""),
            })}
            showClear
            onClear={() => setValues((v) => ({ ...v, owner_user_ids: [] }))}
            onChange={(value) => setValues((v) => ({
              ...v,
              owner_user_ids: Array.isArray(value) ? value.filter((item): item is number => typeof item === "number") : [],
            }))}
          />
        </label>
        <label>
          <span>Sandbox Container</span>
          <Select
            prefix={<Server size={16} />}
            value={values.sandbox_container_id ?? undefined}
            optionList={sandboxOptionList}
            placeholder={sandboxLoading ? "Loading sandbox containers" : "Select sandbox container"}
            emptyContent={sandboxLoading ? <Spin size="small" /> : "No running sandbox containers"}
            loading={sandboxLoading}
            showClear
            renderSelectedItem={() => selectedSandbox ? selectedSandbox.container_name : ""}
            onClear={() => setValues((v) => ({ ...v, sandbox_container_id: null }))}
            onChange={(value) => setValues((v) => ({ ...v, sandbox_container_id: typeof value === "number" ? value : null }))}
          />
        </label>
      </div>

      <label>
        <span>Description</span>
        <TextArea value={values.description} maxLength={2000} autosize={{ minRows: 3, maxRows: 6 }}
          onChange={(description) => setValues((v) => ({ ...v, description }))}
        />
      </label>

      <section className="project-assets-editor">
        <header>
          <span>Assets</span>
          <Button
            icon={<Plus size={14} />}
            size="small"
            theme="borderless"
            onClick={() => setValues((v) => ({ ...v, assets: [...v.assets, { ...EMPTY_ASSET }] }))}
          >
            Add Asset
          </Button>
        </header>
        <div className="project-assets-rows">
          {values.assets.map((asset, index) => (
            <article key={index} className="project-asset-row">
              <label>
                <span>Type</span>
                <Select
                  value={asset.type}
                  disabled={Boolean(asset.existing_id)}
                  optionList={assetTypes.map((type) => ({ label: WORK_PROJECT_ASSET_TYPE_LABEL[type], value: type }))}
                  onChange={(type) => isWorkProjectAssetType(type) && updateAsset(index, resetAssetForType(type))}
                />
              </label>
              {asset.type === WORK_PROJECT_ASSET_TYPE.BINARY ? (
                <label className="project-asset-base-field">
                  <span>Path</span>
                  <Input
                    value={asset.path}
                    maxLength={500}
                    disabled={Boolean(asset.existing_id)}
                    required
                    onChange={(path) => updateAsset(index, { path })}
                  />
                </label>
              ) : (
                <>
                  <label>
                    <span>{ASSET_HOST_FIELD_LABEL[asset.type]}</span>
                    <Input value={asset.host} maxLength={255} onChange={(host) => updateAsset(index, { host })} />
                  </label>
                  {asset.type === WORK_PROJECT_ASSET_TYPE.SERVICE ? (
                    <label>
                      <span>Port</span>
                      <InputNumber value={asset.port ?? undefined} min={1} max={65535} onChange={(port) => updateAsset(index, { port: typeof port === "number" ? port : null })} />
                    </label>
                  ) : null}
                </>
              )}
              <Button
                icon={<Trash2 size={14} />}
                theme="borderless"
                type="danger"
                disabled={values.assets.length <= 1 || Boolean(asset.existing_id)}
                aria-label="Remove asset"
                onClick={() => removeAsset(index)}
              />
            </article>
          ))}
        </div>
      </section>
    </ResourceModal>
  );
}

function UserOption({ user }: { user: SystemUser }) {
  return (
    <div className="project-user-option">
      <span>{user.username}</span>
      <small>{user.email || "No email"}</small>
      <Tag color={SYSTEM_USER_ROLE_COLOR[user.role]}>{SYSTEM_USER_ROLE_LABEL[user.role]}</Tag>
    </div>
  );
}

function SandboxContainerOption({ container }: { container: SandboxContainer }) {
  return (
    <div className="project-sandbox-option">
      <span>{container.container_name}</span>
      <small>ID: {container.id} · {container.container_hash || "Pending hash"}</small>
      <Tag color={SANDBOX_CONTAINER_STATUS_COLOR[container.status]}>
        {SANDBOX_CONTAINER_STATUS_LABEL[container.status]}
      </Tag>
    </div>
  );
}

function assetFromProject(asset: WorkProject["assets"][number]): AssetFormRow {
  return {
    existing_id: asset.id,
    type: asset.type,
    path: asset.path,
    host: asset.host,
    port: asset.port,
  };
}

function normalizeAsset(asset: AssetFormRow): WorkProjectAssetRequest {
  if (asset.type === WORK_PROJECT_ASSET_TYPE.BINARY) {
    return { type: asset.type, path: asset.path.trim(), host: "", port: null };
  }
  return {
    type: asset.type,
    path: "",
    host: asset.host.trim(),
    port: asset.type === WORK_PROJECT_ASSET_TYPE.SERVICE ? asset.port : null,
  };
}

function isAssetComplete(asset: WorkProjectAssetRequest): boolean {
  if (asset.type === WORK_PROJECT_ASSET_TYPE.BINARY) return Boolean(asset.path.trim());
  return Boolean(asset.host.trim());
}

function resetAssetForType(type: WorkProjectAssetRequest["type"]): Partial<AssetFormRow> {
  return { type, path: "", host: "", port: null };
}

// Label for the `host` input field, which carries a different identifier per asset type.
const ASSET_HOST_FIELD_LABEL: Record<Exclude<WorkProjectAssetRequest["type"], "binary">, string> = {
  service: "Host",
  domain: "Domain",
  network: "Network (CIDR)",
};
