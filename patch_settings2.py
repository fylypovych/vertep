import re

path = "web-v2/src/app/settings/settings.component.ts"
with open(path, "r", encoding="utf-8") as f:
    lines = f.readlines()

# Find and replace the backup methods section
new_lines = []
i = 0
while i < len(lines):
    line = lines[i]
    
    # Replace loadBackups + createBackup + restoreBackup + recoverToNormal block
    if line.strip() == "loadBackups(): void {":
        # Find the end of recoverToNormal
        block_start = i
        brace_count = 0
        j = i
        while j < len(lines):
            if "{" in lines[j]:
                brace_count += lines[j].count("{")
            if "}" in lines[j]:
                brace_count -= lines[j].count("}")
            if brace_count == 0 and j > i:
                break
            j += 1
        
        # Write replacement block
        new_lines.append("  loadBackups(): void {\n")
        new_lines.append("    this.backupsLoading.set(true);\n")
        new_lines.append("    this.backupsError.set(null);\n")
        new_lines.append("    this.api.getBackups().subscribe({\n")
        new_lines.append("      next: (data) => { this.backups.set((data['snapshots'] as BackupInfo[]) || []); this.backupsLoading.set(false); },\n")
        new_lines.append("      error: (err) => { this.backupsError.set(err.message); this.backupsLoading.set(false); },\n")
        new_lines.append("    });\n")
        new_lines.append("  }\n")
        new_lines.append("\n")
        new_lines.append("  loadSystemState(): void {\n")
        new_lines.append("    this.systemStateLoading.set(true);\n")
        new_lines.append("    this.api.getSystemState().subscribe({\n")
        new_lines.append("      next: (state) => { this.systemState.set(state as Record<string, unknown>); this.systemStateLoading.set(false); },\n")
        new_lines.append("      error: () => { this.systemStateLoading.set(false); },\n")
        new_lines.append("    });\n")
        new_lines.append("  }\n")
        new_lines.append("\n")
        new_lines.append("  createBackup(): void {\n")
        new_lines.append("    this.api.createBackup().subscribe({\n")
        new_lines.append("      next: () => { this.toast.show('Бекап створено', 'success'); this.loadBackups(); },\n")
        new_lines.append("      error: (err) => this.backupsError.set(err.message),\n")
        new_lines.append("    });\n")
        new_lines.append("  }\n")
        new_lines.append("\n")
        new_lines.append("  confirmRestore(snapshotId: string): void {\n")
        new_lines.append("    const state = this.systemState();\n")
        new_lines.append("    if (state && state['state'] && !['MAINTENANCE', 'READ_ONLY', 'RECOVERING'].includes(state['state'] as string)) {\n")
        new_lines.append("      this.backupsError.set(`Відновлення заборонено в стані ${state['state']}. Перемкніть систему у MAINTENANCE/READ_ONLY/RECOVERING.`);\n")
        new_lines.append("      return;\n")
        new_lines.append("    }\n")
        new_lines.append("    if (!confirm(`Відновити бекап ${snapshotId}? Ця дія необоротна.`)) return;\n")
        new_lines.append("    this.restoring.set(snapshotId);\n")
        new_lines.append("    this.restoreProgress.set({ progress: 0, message: 'Запуск відновлення...' });\n")
        new_lines.append("    this.api.restoreBackup(snapshotId).subscribe({\n")
        new_lines.append("      next: () => this.pollRestoreProgress(snapshotId),\n")
        new_lines.append("      error: (err) => { this.backupsError.set(err.message); this.restoring.set(null); this.restoreProgress.set(null); },\n")
        new_lines.append("    });\n")
        new_lines.append("  }\n")
        new_lines.append("\n")
        new_lines.append("  private pollRestoreProgress(snapshotId: string, attempts = 0): void {\n")
        new_lines.append("    const maxAttempts = 120;\n")
        new_lines.append("    if (attempts >= maxAttempts) {\n")
        new_lines.append("      this.backupsError.set('Перевищено час очікування відновлення');\n")
        new_lines.append("      this.restoring.set(null);\n")
        new_lines.append("      this.restoreProgress.set(null);\n")
        new_lines.append("      this.loadBackups();\n")
        new_lines.append("      return;\n")
        new_lines.append("    }\n")
        new_lines.append("    setTimeout(() => {\n")
        new_lines.append("      this.api.getRestoreProgress(snapshotId).subscribe({\n")
        new_lines.append("        next: (data) => {\n")
        new_lines.append("          const progress = ((data['progress'] as number) || 0);\n")
        new_lines.append("          const message = (data['message'] as string) || 'Відновлення...';\n")
        new_lines.append("          const status = (data['status'] as string) || 'unknown';\n")
        new_lines.append("          this.restoreProgress.set({ progress, message });\n")
        new_lines.append("          if (status === 'done') {\n")
        new_lines.append("            this.toast.show('Відновлення завершено', 'success');\n")
        new_lines.append("            this.restoring.set(null);\n")
        new_lines.append("            this.restoreProgress.set(null);\n")
        new_lines.append("            this.loadBackups();\n")
        new_lines.append("            this.loadSystemState();\n")
        new_lines.append("          } else if (status === 'error') {\n")
        new_lines.append("            this.backupsError.set(message || 'Помилка відновлення');\n")
        new_lines.append("            this.restoring.set(null);\n")
        new_lines.append("            this.restoreProgress.set(null);\n")
        new_lines.append("            this.loadBackups();\n")
        new_lines.append("          } else {\n")
        new_lines.append("            this.pollRestoreProgress(snapshotId, attempts + 1);\n")
        new_lines.append("          }\n")
        new_lines.append("        },\n")
        new_lines.append("        error: () => this.pollRestoreProgress(snapshotId, attempts + 1),\n")
        new_lines.append("      });\n")
        new_lines.append("    }, 2000);\n")
        new_lines.append("  }\n")
        new_lines.append("\n")
        new_lines.append("  recoverToNormal(): void {\n")
        new_lines.append("    this.api.recoverToNormal().subscribe({\n")
        new_lines.append("      next: () => { this.toast.show('Система переведена в NORMAL', 'success'); this.loadUpdateStatus(); this.loadSystemState(); },\n")
        new_lines.append("      error: (err) => this.updateError.set(err.message),\n")
        new_lines.append("    });\n")
        new_lines.append("  }\n")
        i = j + 1
        continue
    
    # Remove old confirmRestore at end of file (before formatBytes)
    if line.strip() == "confirmRestore(snapshotId: string): void {" and i > 1000:
        # Skip until we hit formatBytes
        while i < len(lines) and lines[i].strip() != "formatBytes(bytes: number): string {":
            i += 1
        continue
    
    new_lines.append(line)
    i += 1

# Add isRestoreAllowed before formatBytes
final_lines = []
for i, line in enumerate(new_lines):
    final_lines.append(line)
    if line.strip() == "formatBytes(bytes: number): string {":
        # Insert isRestoreAllowed before this line
        final_lines.insert(-1, "  isRestoreAllowed(): boolean {\n")
        final_lines.insert(-1, "    const state = this.systemState();\n")
        final_lines.insert(-1, "    if (!state) return false;\n")
        final_lines.insert(-1, "    return ['MAINTENANCE', 'READ_ONLY', 'RECOVERING'].includes(state['state'] as string);\n")
        final_lines.insert(-1, "  }\n")
        final_lines.insert(-1, "\n")

with open(path, "w", encoding="utf-8") as f:
    f.writelines(final_lines)

print("Done")
