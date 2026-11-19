"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { mutate } from "swr";
import {
  Button,
  Card,
  InputSwitch,
  InputSingleSelect,
  InputTextArea,
  InputTypeIn,
  InputTypeInTag,
  MessageCard,
  Modal,
  Text,
  type SelectOption,
  type TagItem,
} from "@opal/components";
import { SvgEdit, SvgPlus, SvgTrash } from "@opal/icons";
import SvgNoResult from "@opal/illustrations/no-result";
import {
  ContentAction,
  IllustrationContent,
  InputHorizontal,
  InputVertical,
  Section,
  SettingsLayouts,
  toast,
} from "@opal/layouts";
import { useAdminRouteTitle } from "@/lib/adminNavLabels";
import { ADMIN_ROUTES } from "@/lib/admin-routes";
import { SWR_KEYS } from "@/lib/swr-keys";
import type { StandardAnswer, StandardAnswerCategory } from "@/lib/types";
import {
  useStandardAnswerCategories,
  useStandardAnswers,
} from "@/lib/standardAnswers/hooks";
import {
  createStandardAnswer,
  createStandardAnswerCategory,
  deleteStandardAnswer,
  type StandardAnswerInput,
  updateStandardAnswer,
} from "@/lib/standardAnswers/svc";

const route = ADMIN_ROUTES.STANDARD_ANSWERS;

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

interface StandardAnswerModalProps {
  existing: StandardAnswer | null;
  categories: StandardAnswerCategory[];
  onClose: () => void;
}

function StandardAnswerModal({
  existing,
  categories,
  onClose,
}: StandardAnswerModalProps) {
  const t = useTranslations("admin.standardAnswers");
  const tSelect = useTranslations("common.inputSelect");
  const [keyword, setKeyword] = useState(existing?.keyword ?? "");
  const [answer, setAnswer] = useState(existing?.answer ?? "");
  const [matchRegex, setMatchRegex] = useState(existing?.match_regex ?? false);
  const [matchAny, setMatchAny] = useState(
    existing?.match_any_keywords ?? false
  );
  const [selected, setSelected] = useState<StandardAnswerCategory[]>(
    existing?.categories ?? []
  );
  const [categoryInput, setCategoryInput] = useState("");
  const [saving, setSaving] = useState(false);

  const strategyOptions: SelectOption[] = [
    { value: "all", title: t("form.strategy.all.label") },
    { value: "any", title: t("form.strategy.any.label") },
  ];

  async function addCategory(value: string) {
    const name = value.trim();
    setCategoryInput("");
    if (!name || selected.some((c) => c.name === name)) return;
    const known = categories.find((c) => c.name === name);
    if (known) {
      setSelected((previous) => [...previous, known]);
      return;
    }
    try {
      const created = await createStandardAnswerCategory(name);
      await mutate(SWR_KEYS.standardAnswerCategories);
      setSelected((previous) => [...previous, created]);
    } catch (error) {
      toast.error(
        t("form.toasts.categoryCreateFailed.message", {
          name,
          error: errorText(error),
        })
      );
    }
  }

  const validation = !keyword.trim()
    ? t("form.validation.keywordRequired")
    : !answer.trim()
      ? t("form.validation.answerRequired")
      : selected.length === 0
        ? t("form.validation.categoryRequired")
        : null;

  async function save() {
    if (validation) return;
    const input: StandardAnswerInput = {
      keyword,
      answer,
      categories: selected.map((category) => category.id),
      match_regex: matchRegex,
      match_any_keywords: matchAny,
    };
    setSaving(true);
    try {
      if (existing) {
        await updateStandardAnswer(existing.id, input);
      } else {
        await createStandardAnswer(input);
      }
      await mutate(SWR_KEYS.standardAnswers);
      onClose();
    } catch (error) {
      toast.error(
        t(
          existing
            ? "form.toasts.updateFailed.message"
            : "form.toasts.createFailed.message",
          { error: errorText(error) }
        )
      );
    } finally {
      setSaving(false);
    }
  }

  const tags: TagItem[] = selected.map((category) => ({
    id: String(category.id),
    label: category.name,
  }));

  return (
    <Modal open onOpenChange={onClose}>
      <Modal.Content width="md" height="fit">
        <Modal.Header
          icon={existing ? SvgEdit : SvgPlus}
          title={existing ? t("editPage.title") : t("newStandardAnswer.label")}
          onClose={onClose}
        />
        <Modal.Body>
          <Section gap={1} alignItems="stretch">
            <InputHorizontal
              title={t("form.matchRegex.label")}
              description={t("form.matchRegex.description")}
              withLabel
            >
              <InputSwitch
                checked={matchRegex}
                onCheckedChange={setMatchRegex}
              />
            </InputHorizontal>
            <InputVertical
              title={
                matchRegex
                  ? t("form.regexPattern.label")
                  : matchAny
                    ? t("form.anyKeywords.label")
                    : t("form.allKeywords.label")
              }
              withLabel
            >
              <InputTypeIn
                value={keyword}
                placeholder={
                  matchRegex
                    ? t("placeholders.matchRegex")
                    : matchAny
                      ? t("form.anyKeywords.placeholder")
                      : t("form.allKeywords.placeholder")
                }
                onChange={(event) => setKeyword(event.target.value)}
              />
            </InputVertical>
            {!matchRegex && (
              <InputVertical
                title={t("form.strategy.label")}
                description={t("form.strategy.description")}
              >
                <InputSingleSelect
                  value={matchAny ? "any" : "all"}
                  onValueChange={(value) => setMatchAny(value === "any")}
                  options={strategyOptions}
                  placeholder={tSelect("placeholder.fallback")}
                />
              </InputVertical>
            )}
            <InputVertical title={t("form.answer.label")} withLabel>
              <InputTextArea
                rows={5}
                value={answer}
                placeholder={t("form.answer.placeholder")}
                onChange={(event) => setAnswer(event.target.value)}
              />
            </InputVertical>
            <InputVertical title={t("form.categories.label")}>
              <InputTypeInTag
                tags={tags}
                value={categoryInput}
                onChange={setCategoryInput}
                onAdd={(value) => void addCategory(value)}
                onRemoveTag={(id) =>
                  setSelected((previous) =>
                    previous.filter((category) => String(category.id) !== id)
                  )
                }
                placeholder={t("form.categories.placeholder")}
              />
            </InputVertical>
            {validation && (
              <Text as="p" font="secondary-body" color="text-03">
                {validation}
              </Text>
            )}
          </Section>
        </Modal.Body>
        <Modal.Footer>
          <Button
            disabled={saving || validation !== null}
            onClick={() => void save()}
          >
            {existing
              ? t("form.updateButton.label")
              : t("form.createButton.label")}
          </Button>
        </Modal.Footer>
      </Modal.Content>
    </Modal>
  );
}

export default function StandardAnswersPage() {
  const t = useTranslations("admin.standardAnswers");
  const tDelete = useTranslations("common.deleteButton");
  const adminRouteTitle = useAdminRouteTitle();
  const answers = useStandardAnswers();
  const categories = useStandardAnswerCategories();
  const [editing, setEditing] = useState<StandardAnswer | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [query, setQuery] = useState("");

  async function remove(answer: StandardAnswer) {
    try {
      await deleteStandardAnswer(answer.id);
      await mutate(SWR_KEYS.standardAnswers);
      toast.success(t("toasts.deleted.message", { id: answer.id }));
    } catch (error) {
      toast.error(
        t("toasts.deleteFailed.message", { error: errorText(error) })
      );
    }
  }

  const needle = query.trim().toLowerCase();
  const visible = (answers.data ?? []).filter(
    (answer) =>
      !needle ||
      answer.keyword.toLowerCase().includes(needle) ||
      answer.answer.toLowerCase().includes(needle)
  );

  return (
    <SettingsLayouts.Root loading={answers.isLoading || categories.isLoading}>
      <SettingsLayouts.Header
        icon={route.icon}
        title={adminRouteTitle(route)}
        description={t("intro.description")}
        divider
        actions={[
          <Button
            key="new"
            icon={SvgPlus}
            onClick={() => {
              setEditing(null);
              setModalOpen(true);
            }}
          >
            {t("newStandardAnswer.label")}
          </Button>,
        ]}
      />
      <SettingsLayouts.Body>
        {answers.error || categories.error ? (
          <MessageCard
            variant="error"
            title={t("errors.loadAnswersFailed.title")}
            description={t("errors.fetchAnswersFailed.message")}
          />
        ) : (
          <Section gap={0.5} alignItems="stretch">
            <InputTypeIn
              value={query}
              placeholder={t("search.placeholder")}
              onChange={(event) => setQuery(event.target.value)}
            />
            {visible.length === 0 ? (
              <IllustrationContent
                illustration={SvgNoResult}
                title={
                  needle
                    ? t("table.empty.message")
                    : t("intro.addFirst.message")
                }
              />
            ) : (
              visible.map((answer) => (
                <Card key={answer.id} border="solid" rounding={4}>
                  <ContentAction
                    font="main-ui-action"
                    title={answer.keyword}
                    suffix={answer.categories.map((c) => c.name).join(", ")}
                    description={answer.answer}
                    padding={1}
                    rightChildren={
                      <Section flexDirection="row" width="fit" gap={0.25}>
                        <Button
                          icon={SvgEdit}
                          prominence="tertiary"
                          tooltip={t("editPage.title")}
                          onClick={() => {
                            setEditing(answer);
                            setModalOpen(true);
                          }}
                        />
                        <Button
                          icon={SvgTrash}
                          prominence="tertiary"
                          tooltip={tDelete("button.tooltip")}
                          onClick={() => void remove(answer)}
                        />
                      </Section>
                    }
                  />
                </Card>
              ))
            )}
          </Section>
        )}
        {modalOpen && (
          <StandardAnswerModal
            existing={editing}
            categories={categories.data ?? []}
            onClose={() => setModalOpen(false)}
          />
        )}
      </SettingsLayouts.Body>
    </SettingsLayouts.Root>
  );
}
