/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Search, Settings2, X } from "lucide-react";
import { useState } from "react";
import { AvatarStack, Money, Section, StatusBadge } from "~/components/kit";
import { DeliverableDial, type DeliverableChoice } from "~/components/composer/dials";
import {
  AlertDialog,
  AlertDialogActions,
  AlertDialogBody,
  AlertDialogClose,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogHeader,
  AlertDialogIntro,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@makeplane/propel/components/alert-dialog";
import { Avatar } from "@makeplane/propel/components/avatar";
import { Badge, type BadgeVariant } from "@makeplane/propel/components/badge";
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@makeplane/propel/components/breadcrumb";
import { Button, type ButtonVariant } from "@makeplane/propel/components/button";
import { Checkbox } from "@makeplane/propel/components/checkbox";
import {
  Combobox,
  ComboboxContent,
  ComboboxItem,
  ComboboxList,
  ComboboxSearch,
  ComboboxStatus,
  ComboboxTrigger,
} from "@makeplane/propel/components/combobox";
import {
  Dialog,
  DialogActions,
  DialogClose,
  DialogCloseGroup,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogHeading,
  DialogMain,
  DialogTitle,
  DialogTrigger,
} from "@makeplane/propel/components/dialog";
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import { InputField } from "@makeplane/propel/components/input-field";
import { LinearProgress } from "@makeplane/propel/components/linear-progress";
import {
  Menu,
  MenuContent,
  MenuGroup,
  MenuItem,
  MenuLabel,
  MenuSeparator,
  MenuTrigger,
} from "@makeplane/propel/components/menu";
import {
  Popover,
  PopoverContent,
  PopoverDescription,
  PopoverTitle,
  PopoverTrigger,
} from "@makeplane/propel/components/popover";
import {
  RadioGroupField,
  RadioGroupFieldOption,
} from "@makeplane/propel/components/radio-group-field";
import { SelectField } from "@makeplane/propel/components/select-field";
import { Separator } from "@makeplane/propel/components/separator";
import { Skeleton, SkeletonItem } from "@makeplane/propel/components/skeleton";
import { Switch } from "@makeplane/propel/components/switch";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@makeplane/propel/components/table";
import { Tab, Tabs, TabsList, TabsPanel } from "@makeplane/propel/components/tabs";
import { TextAreaField } from "@makeplane/propel/components/text-area-field";
import { Tooltip } from "@makeplane/propel/components/tooltip";
import { Specimen } from "./shared";

function DeliverableDialSpecimen() {
  // Live state so the census exercises the real gesture: pick a format in the flyout and
  // the pill re-labels ("Document · PDF"), exactly as the composer's does.
  const [choice, setChoice] = useState<DeliverableChoice>("auto");
  return <DeliverableDial value={choice} onChange={setChoice} />;
}

const BUTTON_VARIANTS: ButtonVariant[] = [
  "primary",
  "secondary",
  "tertiary",
  "ghost",
  "danger",
  "danger-outline",
];
const BADGE_VARIANTS: BadgeVariant[] = ["neutral", "brand", "info", "success", "warning", "danger"];
const MODELS = [
  { value: "opus", label: "Claude Opus" },
  { value: "sonnet", label: "Claude Sonnet" },
  { value: "gemini", label: "Gemini Pro" },
];
const FORMATS = ["Markdown", "PDF", "HTML page"].map((f) => ({ value: f, label: f }));
const SCREENS = ["Home", "Tasks", "Projects", "Inbox", "Connectors"];

/** The project picker's shape (composer/project-picker.tsx), filtering a fixed list. */
function ComboboxSpecimen() {
  const [query, setQuery] = useState("");
  const [value, setValue] = useState<string | null>(null);
  const shown = SCREENS.filter((s) => s.toLowerCase().includes(query.trim().toLowerCase()));
  return (
    <Combobox
      modal={false}
      filter={null}
      autoHighlight
      inputValue={query}
      onInputValueChange={setQuery}
      value={value}
      onValueChange={setValue}
    >
      <ComboboxTrigger
        size="md"
        variant="secondary"
        width="fit"
        icon={<Icon icon={Search} />}
        aria-label="Jump to"
      >
        <span>{value ?? "Jump to…"}</span>
      </ComboboxTrigger>
      <ComboboxContent
        align="start"
        sizing="auto-fixed"
        aria-label="Screens"
        search={<ComboboxSearch placeholder="Jump to…" />}
      >
        <ComboboxList>
          {shown.map((s) => (
            <ComboboxItem key={s} value={s} label={s} />
          ))}
        </ComboboxList>
        <ComboboxStatus>{shown.length === 0 ? "No results." : null}</ComboboxStatus>
      </ComboboxContent>
    </Combobox>
  );
}

function PropelBasics() {
  return (
    <>
      <Specimen label="Button · variants" file="components/button">
        {BUTTON_VARIANTS.map((v) => (
          <Button key={v} variant={v} size="md" stretch="auto" label={v} />
        ))}
      </Specimen>
      <Specimen label="Button · sizes" file="components/button · icon-button">
        {(["xs", "sm", "md", "lg"] as const).map((s) => (
          <Button key={s} variant="secondary" size={s} stretch="auto" label={s} />
        ))}
        <IconButton
          variant="ghost"
          size="md"
          aria-label="Settings"
          icon={<Icon icon={Settings2} />}
        />
      </Specimen>
      <Specimen label="Badge" file="components/badge" note="Kind, role and family labels.">
        {BADGE_VARIANTS.map((v) => (
          <Badge key={v} variant={v} size="sm" label={v} />
        ))}
      </Specimen>
      <Specimen label="Fields" file="components/input-field · text-area-field">
        <div className="w-full max-w-xs space-y-3">
          <InputField size="lg" orientation="vertical" label="Name" placeholder="Type here…" />
          <TextAreaField size="lg" label="Notes" placeholder="A longer thought…" />
        </div>
      </Specimen>
      <Specimen
        label="SelectField"
        file="components/select-field"
        note="Two side by side on purpose: open one, press the other, and exactly one popup stays open."
      >
        <SelectField<string>
          modal={false}
          size="md"
          variant="secondary"
          width="fit"
          aria-label="Model"
          defaultValue="opus"
          options={MODELS}
        />
        <SelectField<string>
          modal={false}
          size="md"
          variant="secondary"
          width="fit"
          aria-label="Format"
          defaultValue="Markdown"
          options={FORMATS}
        />
      </Specimen>
      <Specimen label="DeliverableDial" file="composer/dials.tsx">
        {/* The composer's path dial, here because the composer sits behind login:
                  the nested Document family (click = plain document; chevron = formats)
                  must stay click-provable on a public page. */}
        <DeliverableDialSpecimen />
      </Specimen>
      <Specimen
        label="Checkbox · Radio · Switch"
        file="components/checkbox · radio-group-field · switch"
      >
        <label className="flex items-center gap-2 text-body-sm-regular">
          <Checkbox defaultChecked /> verified only
        </label>
        <RadioGroupField
          size="md"
          density="compact"
          label="Lane"
          name="ds-lane"
          defaultValue="plan"
        >
          <RadioGroupFieldOption value="plan" label="plan" />
          <RadioGroupFieldOption value="fleet" label="fleet" />
        </RadioGroupField>
        <Switch size="md" defaultChecked aria-label="Notify me" />
        <Switch size="sm" aria-label="Quiet hours" />
      </Specimen>
      <Specimen label="Tabs" file="components/tabs">
        <div className="w-full max-w-sm">
          <Tabs variant="underline" defaultValue="tasks">
            <TabsList>
              <Tab value="tasks" label="Tasks" />
              <Tab value="artifacts" label="Artifacts" />
              <Tab value="settings" label="Settings" />
            </TabsList>
            {(["tasks", "artifacts", "settings"] as const).map((t) => (
              <TabsPanel key={t} value={t}>
                <p className="text-muted-foreground pt-2 text-body-sm-regular">The {t} tab.</p>
              </TabsPanel>
            ))}
          </Tabs>
        </div>
      </Specimen>
      <Specimen label="Skeleton · LinearProgress" file="components/skeleton · linear-progress">
        <div className="w-48 space-y-2">
          <Skeleton aria-label="Loading two lines" layout="stack">
            <SkeletonItem variant="bar" blockSize="1rem" inlineSize="75%" />
            <SkeletonItem variant="bar" blockSize="1rem" />
          </Skeleton>
          <LinearProgress
            value={64}
            size="sm"
            variant="brand"
            showValue={false}
            aria-label="Budget spent"
          />
        </div>
      </Specimen>
      <Specimen label="Avatar · AvatarStack" file="components/avatar · kit/avatar-stack.tsx">
        <Avatar size="md" alt="Ada Lovelace" fallback="AL" />
        <AvatarStack
          max={3}
          people={[
            { id: "pc", name: "Pat Chen" },
            { id: "al", name: "Ada Lovelace" },
            { id: "gh", name: "Grace Hopper", dim: true },
            { id: "kj", name: "Katherine Johnson" },
          ]}
        />
      </Specimen>
    </>
  );
}

function PropelOverlays() {
  return (
    <>
      <Specimen label="Breadcrumb" file="components/breadcrumb">
        <Breadcrumb aria-label="Breadcrumb">
          <BreadcrumbList>
            <BreadcrumbItem>
              <BreadcrumbLink href="#propel" label="Projects" />
            </BreadcrumbItem>
            <BreadcrumbSeparator />
            <BreadcrumbItem>
              <BreadcrumbPage label="Q3 planning" />
            </BreadcrumbItem>
          </BreadcrumbList>
        </Breadcrumb>
      </Specimen>
      <Specimen label="Tooltip" file="components/tooltip">
        <Tooltip label="A quiet hint" side="top" align="center">
          <Button variant="secondary" size="sm" stretch="auto" label="Hover me" />
        </Tooltip>
      </Specimen>
      <Specimen label="Menu" file="components/menu">
        <Menu>
          <MenuTrigger
            render={<Button variant="secondary" size="sm" stretch="auto" label="Open menu" />}
          />
          <MenuContent align="start">
            <MenuGroup>
              <MenuLabel>Task</MenuLabel>
              <MenuItem label="Rename" />
              <MenuItem label="Move to project" />
            </MenuGroup>
            <MenuSeparator />
            <MenuItem variant="danger" label="Delete" />
          </MenuContent>
        </Menu>
      </Specimen>
      <Specimen label="Popover" file="components/popover">
        <Popover>
          <PopoverTrigger
            render={<Button variant="secondary" size="sm" stretch="auto" label="Open popover" />}
          />
          <PopoverContent align="start">
            <div className="w-64 space-y-1">
              <PopoverTitle>Budget</PopoverTitle>
              <PopoverDescription>Explained in place, committed on close.</PopoverDescription>
            </div>
          </PopoverContent>
        </Popover>
      </Specimen>
      <Specimen label="Dialog · AlertDialog" file="components/dialog · alert-dialog">
        <Dialog>
          <DialogTrigger
            render={<Button variant="secondary" size="sm" stretch="auto" label="Open dialog" />}
          />
          <DialogContent size="xs">
            <DialogCloseGroup>
              <IconButton
                variant="ghost"
                size="xs"
                aria-label="Close"
                icon={<Icon icon={X} />}
                render={<DialogClose />}
              />
            </DialogCloseGroup>
            <DialogMain>
              <DialogHeader>
                <DialogHeading>
                  <DialogTitle>A dialog</DialogTitle>
                  <DialogDescription>
                    Propel's anatomy: a close group, the main column, the actions row.
                  </DialogDescription>
                </DialogHeading>
              </DialogHeader>
            </DialogMain>
            <DialogActions>
              <Button
                variant="primary"
                size="md"
                stretch="auto"
                label="Done"
                render={<DialogClose />}
              />
            </DialogActions>
          </DialogContent>
        </Dialog>
        <AlertDialog>
          <AlertDialogTrigger
            render={<Button variant="danger-outline" size="sm" stretch="auto" label="Delete…" />}
          />
          <AlertDialogContent>
            <AlertDialogBody>
              <AlertDialogHeader>
                <AlertDialogIntro>
                  <AlertDialogTitle>Delete this task?</AlertDialogTitle>
                  <AlertDialogDescription>
                    Every yes/no is an AlertDialog: an outside press never dismisses it, Escape
                    does.
                  </AlertDialogDescription>
                </AlertDialogIntro>
              </AlertDialogHeader>
            </AlertDialogBody>
            <AlertDialogActions>
              <Button
                variant="ghost"
                size="md"
                stretch="auto"
                label="Cancel"
                render={<AlertDialogClose />}
              />
              <Button
                variant="danger"
                size="md"
                stretch="auto"
                label="Delete"
                render={<AlertDialogClose />}
              />
            </AlertDialogActions>
          </AlertDialogContent>
        </AlertDialog>
      </Specimen>
      <Specimen label="Combobox" file="components/combobox">
        <ComboboxSpecimen />
      </Specimen>
      <Specimen label="Table" file="components/table">
        <div className="w-full max-w-md">
          <Table variant="table">
            <TableHeader>
              <TableRow>
                <TableHead label="Task" />
                <TableHead label="Status" />
                <TableHead label="Cost" align="end" />
              </TableRow>
            </TableHeader>
            <TableBody>
              <TableRow>
                <TableCell>Q3 memo</TableCell>
                <TableCell>
                  <StatusBadge status="delivered" />
                </TableCell>
                <TableCell align="end">
                  <Money usd="0.0382" />
                </TableCell>
              </TableRow>
              <TableRow selected>
                <TableCell>Board of workstreams</TableCell>
                <TableCell>
                  <StatusBadge status="running" />
                </TableCell>
                <TableCell align="end">
                  <Money usd="0.0104" />
                </TableCell>
              </TableRow>
            </TableBody>
          </Table>
        </div>
      </Specimen>
      <Specimen label="Separator" file="components/separator">
        <div className="flex h-8 items-center gap-3 text-body-sm-regular">
          left
          <Separator orientation="vertical" decorative />
          right
        </div>
      </Specimen>
    </>
  );
}

export function PropelSection() {
  return (
    <section id="propel">
      <Section title="Propel - Plane's primitives (@makeplane/propel)">
        <p className="text-muted-foreground max-w-2xl text-body-sm-regular">
          Imported straight from Propel, as Plane's apps do. Content is a prop, every variant axis
          is passed, and no Propel component takes a className or style: layout goes on a wrapper
          (ADR-074).
        </p>
        <div className="space-y-4">
          <PropelBasics />
          <PropelOverlays />
        </div>
      </Section>
    </section>
  );
}
