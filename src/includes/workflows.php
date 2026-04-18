<?php
/**
 * PostPro Suite — Workflow & Category Definitions
 * Maps workflow IDs to Python scripts and UI metadata.
 */

define('CATEGORIES', [
    [
        'id'       => 'lwde',
        'icon'     => '▶',
        'title'    => 'LWDE',
        'subtitle' => 'Active Workflows',
        'type'     => 'workflows',
    ],
    [
        'id'       => 'lqa',
        'icon'     => '📋',
        'title'    => 'LQA',
        'subtitle' => 'Script Management',
        'type'     => 'scripts',
    ],
]);

define('WORKFLOWS', [
    [
        'id'          => 0,
        'icon'        => 'arrow-down-circle',
        'title'       => 'Download RAW (SKU)',
        'subtitle'    => 'Load images from DAM by SKU / Request Key',
        'script'      => '00-SKU-based-json-2.py',
        'input_label' => 'Request Key / SKU',
        'input_hint'  => 'z. B.  10050196',
    ],
    [
        'id'          => 1,
        'icon'        => 'arrow-down-circle',
        'title'       => 'Download RAW (Category ID)',
        'subtitle'    => 'Load images from DAM by Category ID',
        'script'      => '03-1_DAM-API-Request-Download.py',
        'input_label' => 'Category ID',
        'input_hint'  => 'z. B.  591672',
    ],
    [
        'id'          => 2,
        'icon'        => 'folder',
        'title'       => 'Image Classification',
        'subtitle'    => 'Sort, rename & classify images',
        'script'      => '02-1_filenaming.py',
        'input_label' => null,
        'input_hint'  => null,
    ],
    [
        'id'          => 3,
        'icon'        => 'check-square',
        'title'       => 'Jira Ticket Completion',
        'subtitle'    => 'Update Jira ticket, generate web images & close',
        'script'      => '04-1_Jira-Final.py',
        'input_label' => 'Ticket-Nummer',
        'input_hint'  => 'z. B.  5175',
    ],
    [
        'id'          => 4,
        'icon'        => 'cloud-upload',
        'title'       => 'Upload to DAM',
        'subtitle'    => 'Clear upload folder, push images directly to DAM (category via Jira ticket)',
        'script'      => '10-2_Upload-DAM-Direct.py',
        'input_label' => null,
        'input_hint'  => null,
    ],
]);

function get_categories(): array {
    return CATEGORIES;
}

function get_category(string $id): ?array {
    foreach (CATEGORIES as $cat) {
        if ($cat['id'] === $id) return $cat;
    }
    return null;
}

function get_workflows(): array {
    return WORKFLOWS;
}

function get_workflow(int $id): ?array {
    foreach (WORKFLOWS as $wf) {
        if ($wf['id'] === $id) return $wf;
    }
    return null;
}
