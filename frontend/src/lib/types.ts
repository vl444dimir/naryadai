export type Role='worker'|'master'|'leader'|'admin';
export interface User{id:string;login:string;full_name:string;role:Role;specialty:string;grade:number;brigade_id:string|null;on_shift:boolean;active:boolean;[key:string]:any}
export interface CatalogItem{id:string;name?:string;[key:string]:any}
export interface Order{id:string;number:number;status:string;version:number;description:string;work_type:string;priority:string;equipment_id:string;section_id:string;executor_id:string|null;working_executor_id:string|null;brigade_id:string|null;master_id:string;due_at:string;norm_minutes:number;overdue:boolean;overdue_minutes:number;result_version:number;equipment?:{id:string;name:string;inventory_number:string};photos:any[];events:any[];assessment:any;[key:string]:any}
export interface OrderList{items:Order[];total:number;counts_by_status:Record<string,number>;overdue_count:number;offset:number;limit:number}
export const STATUS:Record<string,string>={issued:'Новый',accepted:'Принят',queued:'В очереди',rejected:'Отклонён',in_progress:'В работе',paused:'На паузе',performed:'Выполнен',ai_review:'На проверке',rework:'На доработке',closed:'Закрыт',cancelled:'Отменён'};
export const PRIORITY:Record<string,string>={emergency:'Аварийный',high:'Срочный',normal:'Обычный',planned:'Плановый'};
export const ROLE:Record<Role,string>={worker:'Рабочий',master:'Мастер',leader:'Руководитель',admin:'Администратор'};
export const dateTime=(value:string|undefined|null)=>value?new Date(value).toLocaleString('ru-RU',{day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',timeZone:'Asia/Qyzylorda'}):'—';
