<script setup lang="ts">
import {ref} from 'vue'
defineProps<{label:string;value:unknown}>()
const open=ref(false)
</script>
<template><view class="evidence"><button @click="open=!open">{{ open?'▾':'▸' }} {{ label }}</button><text v-if="open" selectable class="json">{{ JSON.stringify(value,null,2) }}</text></view></template>
<style scoped>.evidence{border:1px solid #e8e0d7;border-radius:9px;margin:8px 0;background:#fffdf9}.evidence button{background:none;text-align:left;font-size:11px;padding:8px 10px;margin:0;line-height:1.5;color:#735744}.evidence button::after{border:0}.json{display:block;white-space:pre-wrap;overflow-wrap:anywhere;max-height:340px;overflow:auto;padding:12px;font:11px/1.7 ui-monospace,monospace;color:#574b42;border-top:1px dashed #e8e0d7}</style>
