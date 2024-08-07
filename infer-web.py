import os
import shutil
import sys

import json # Mangio fork using json for preset saving

now_dir = os.getcwd()
sys.path.append(now_dir)
import traceback, pdb
import warnings

import numpy as np
import torch

os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["no_proxy"] = "localhost, 127.0.0.1, ::1"
import logging
import threading
from random import shuffle
from subprocess import Popen
from time import sleep

import faiss
import ffmpeg
import gradio as gr
import soundfile as sf
from config import Config
from fairseq import checkpoint_utils
from i18n import I18nAuto
from lib.infer_pack.models import (
    SynthesizerTrnMs256NSFsid,
    SynthesizerTrnMs256NSFsid_nono,
    SynthesizerTrnMs768NSFsid,
    SynthesizerTrnMs768NSFsid_nono,
)
from lib.infer_pack.models_onnx import SynthesizerTrnMsNSFsidM
from infer_uvr5 import _audio_pre_, _audio_pre_new
from MDXNet import MDXNetDereverb
from my_utils import load_audio
from train.process_ckpt import change_info, extract_small_model, merge, show_info
from vc_infer_pipeline import VC
from sklearn.cluster import MiniBatchKMeans

import subprocess

from subprocess import Popen
from mega import Mega
from shutil import make_archive

logging.getLogger("numba").setLevel(logging.WARNING)


tmp = os.path.join(now_dir, "TEMP")
shutil.rmtree(tmp, ignore_errors=True)
shutil.rmtree("%s/runtime/Lib/site-packages/infer_pack" % (now_dir), ignore_errors=True)
shutil.rmtree("%s/runtime/Lib/site-packages/uvr5_pack" % (now_dir), ignore_errors=True)
os.makedirs(tmp, exist_ok=True)
os.makedirs(os.path.join(now_dir, "logs"), exist_ok=True)
os.makedirs(os.path.join(now_dir, "weights"), exist_ok=True)
os.environ["TEMP"] = tmp
warnings.filterwarnings("ignore")
torch.manual_seed(114514)


config = Config()
i18n = I18nAuto()
i18n.print()
# Determine whether there is N that can be used for training and accelerated reasoning卡
ngpu = torch.cuda.device_count()
gpu_infos = []
mem = []
if_gpu_ok = False

if torch.cuda.is_available() or ngpu != 0:
    for i in range(ngpu):
        gpu_name = torch.cuda.get_device_name(i)
        if any(
            value in gpu_name.upper()
            for value in [
                "10",
                "16",
                "20",
                "30",
                "40",
                "A2",
                "A3",
                "A4",
                "P4",
                "A50",
                "500",
                "A60",
                "70",
                "80",
                "90",
                "M4",
                "T4",
                "TITAN",
            ]
        ):
            # A10#A100#V100#A40#P40#M40#K80#A4500
            if_gpu_ok = True  # 至少有一张能用的N卡
            gpu_infos.append("%s\t%s" % (i, gpu_name))
            mem.append(
                int(
                    torch.cuda.get_device_properties(i).total_memory
                    / 1024
                    / 1024
                    / 1024
                    + 0.4
                )
            )
if if_gpu_ok and len(gpu_infos) > 0:
    gpu_info = "\n".join(gpu_infos)
    default_batch_size = min(mem) // 2
else:
    gpu_info = i18n("很遗憾您这没有能用的显卡来支持您训练")
    default_batch_size = 1
gpus = "-".join([i[0] for i in gpu_infos])


class ToolButton(gr.Button, gr.components.FormComponent):
    """Small button with single emoji as text, fits inside gradio forms"""

    def __init__(self, **kwargs):
        super().__init__(variant="tool", **kwargs)

    def get_block_name(self):
        return "button"


hubert_model = None


def load_hubert():
    global hubert_model
    models, _, _ = checkpoint_utils.load_model_ensemble_and_task(
        ["hubert_base.pt"],
        suffix="",
    )
    hubert_model = models[0]
    hubert_model = hubert_model.to(config.device)
    if config.is_half:
        hubert_model = hubert_model.half()
    else:
        hubert_model = hubert_model.float()
    hubert_model.eval()


weight_root = "weights"
weight_uvr5_root = "uvr5_weights"
index_root = "logs"
names = []
for name in os.listdir(weight_root):
    if name.endswith(".pth"):
        names.append(name)
index_paths = []
for root, dirs, files in os.walk(index_root, topdown=False):
    for name in files:
        if name.endswith(".index") and "trained" not in name:
            index_paths.append("%s/%s" % (root, name))
uvr5_names = []
for name in os.listdir(weight_uvr5_root):
    if name.endswith(".pth") or "onnx" in name:
        uvr5_names.append(name.replace(".pth", ""))


def vc_single(
    sid,
    input_audio_path,
    f0_up_key,
    # f0_file,
    f0_method,
    file_index,
    file_index2,
    # file_big_npy,
    index_rate,
    filter_radius,
    resample_sr,
    rms_mix_rate,
    protect,
    crepe_hop_length,
):  # spk_item, input_audio0, vc_transform0,f0_file,f0method0
    global tgt_sr, net_g, vc, hubert_model, version
    if input_audio_path is None:
        return "You need to upload an audio", None
    f0_up_key = int(f0_up_key)
    try:
        audio = load_audio(input_audio_path, 16000)
        audio_max = np.abs(audio).max() / 0.95
        if audio_max > 1:
            audio /= audio_max
        times = [0, 0, 0]
        if not hubert_model:
            load_hubert()
        if_f0 = cpt.get("f0", 1)
        file_index = (
            (
                file_index.strip(" ")
                .strip('"')
                .strip("\n")
                .strip('"')
                .strip(" ")
                .replace("trained", "added")
            )
            if file_index != ""
            else file_index2
        )  # Prevent Xiaobai from making mistakes, and automatically replace it for him
        # file_big_npy = (
        #     file_big_npy.strip(" ").strip('"').strip("\n").strip('"').strip(" ")
        # )
        audio_opt = vc.pipeline(
            hubert_model,
            net_g,
            sid,
            audio,
            input_audio_path,
            times,
            f0_up_key,
            f0_method,
            file_index,
            # file_big_npy,
            index_rate,
            if_f0,
            filter_radius,
            tgt_sr,
            resample_sr,
            rms_mix_rate,
            version,
            protect,
            crepe_hop_length,
            # f0_file=f0_file,
        )
        if tgt_sr != resample_sr >= 16000:
            tgt_sr = resample_sr
        index_info = (
            "Using index:%s." % file_index
            if os.path.exists(file_index)
            else "Index not used."
        )
        return "Success.\n %s\nTime:\n npy:%ss, f0:%ss, infer:%ss" % (
            index_info,
            times[0],
            times[1],
            times[2],
        ), (tgt_sr, audio_opt)
    except:
        info = traceback.format_exc()
        print(info)
        return info, (None, None)


def vc_multi(
    sid,
    dir_path,
    opt_root,
    paths,
    f0_up_key,
    f0_method,
    file_index,
    file_index2,
    # file_big_npy,
    index_rate,
    filter_radius,
    resample_sr,
    rms_mix_rate,
    protect,
    format1,
    crepe_hop_length,
):
    try:
        dir_path = (
            dir_path.strip(" ").strip('"').strip("\n").strip('"').strip(" ")
        )  # Prevent Xiaobai from copying paths with spaces and " and carriage returns at the beginning and end
        opt_root = opt_root.strip(" ").strip('"').strip("\n").strip('"').strip(" ")
        os.makedirs(opt_root, exist_ok=True)
        try:
            if dir_path != "":
                paths = [os.path.join(dir_path, name) for name in os.listdir(dir_path)]
            else:
                paths = [path.name for path in paths]
        except:
            traceback.print_exc()
            paths = [path.name for path in paths]
        infos = []
        for path in paths:
            info, opt = vc_single(
                sid,
                path,
                f0_up_key,
                None,
                f0_method,
                file_index,
                file_index2,
                # file_big_npy,
                index_rate,
                filter_radius,
                resample_sr,
                rms_mix_rate,
                protect,
                crepe_hop_length
            )
            if "Success" in info:
                try:
                    tgt_sr, audio_opt = opt
                    if format1 in ["wav", "flac"]:
                        sf.write(
                            "%s/%s.%s" % (opt_root, os.path.basename(path), format1),
                            audio_opt,
                            tgt_sr,
                        )
                    else:
                        path = "%s/%s.wav" % (opt_root, os.path.basename(path))
                        sf.write(
                            path,
                            audio_opt,
                            tgt_sr,
                        )
                        if os.path.exists(path):
                            os.system(
                                "ffmpeg -i %s -vn %s -q:a 2 -y"
                                % (path, path[:-4] + ".%s" % format1)
                            )
                except:
                    info += traceback.format_exc()
            infos.append("%s->%s" % (os.path.basename(path), info))
            yield "\n".join(infos)
        yield "\n".join(infos)
    except:
        yield traceback.format_exc()

def save_wav_for_vocal(audioFile):
    file_path=audioFile[0].name
    shutil.copy2(file_path,'./audios_user')
    return os.path.join('./audios_user',os.path.basename(file_path))

def uvr(model_name, inp_root, save_root_vocal, paths, save_root_ins, agg, format0):
    infos = []
    if inp_root != "":
        try:
            inp_root = inp_root.strip(" ").strip('"').strip("\n").strip('"').strip(" ")
            save_root_vocal = (
                save_root_vocal.strip(" ").strip('"').strip("\n").strip('"').strip(" ")
            )
            save_root_ins = (
                save_root_ins.strip(" ").strip('"').strip("\n").strip('"').strip(" ")
            )
            if model_name == "onnx_dereverb_By_FoxJoy":
                pre_fun = MDXNetDereverb(15)
            else:
                func = _audio_pre_ if "DeEcho" not in model_name else _audio_pre_new
                pre_fun = func(
                    agg=int(agg),
                    model_path=os.path.join(weight_uvr5_root, model_name + ".pth"),
                    device=config.device,
                    is_half=config.is_half,
                )
            if inp_root != "":
                # paths = [os.path.join(inp_root, name) for name in os.listdir(inp_root)]
                paths[0] = inp_root
                print(paths[0])
            else:
                # paths = [path.name for path in paths]
                paths[0] = inp_root
            for path in paths:
                # inp_path = os.path.join(inp_root, path)
                inp_path = inp_root
                need_reformat = 1
                done = 0
                try:
                    info = ffmpeg.probe(inp_path, cmd="ffprobe")
                    if (
                        info["streams"][0]["channels"] == 2
                        and info["streams"][0]["sample_rate"] == "44100"
                    ):
                        need_reformat = 0
                        pre_fun._path_audio_(
                            inp_path, save_root_ins, save_root_vocal, format0
                        )
                        done = 1
                except:
                    need_reformat = 1
                    traceback.print_exc()
                if need_reformat == 1:
                    tmp_path = "%s/%s.reformatted.wav" % (tmp, os.path.basename(inp_path))
                    os.system(
                        "ffmpeg -i %s -vn -acodec pcm_s16le -ac 2 -ar 44100 %s -y"
                        % (inp_path, tmp_path)
                    )
                    inp_path = tmp_path
                try:
                    if done == 0:
                        pre_fun._path_audio_(
                            inp_path, save_root_ins, save_root_vocal, format0
                        )
                    infos.append("%s->Success" % (os.path.basename(inp_path)))
                    yield "\n".join(infos)
                except:
                    infos.append(
                        "%s->%s" % (os.path.basename(inp_path), traceback.format_exc())
                    )
                    yield "\n".join(infos)
        except:
            infos.append(traceback.format_exc())
            yield "\n".join(infos)
        finally:
            try:
                if model_name == "onnx_dereverb_By_FoxJoy":
                    del pre_fun.pred.model
                    del pre_fun.pred.model_
                else:
                    del pre_fun.model
                    del pre_fun
            except:
                traceback.print_exc()
            print("clean_empty_cache")
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        yield "\n".join(infos)
    else:
        yield "No audio file uplaoded!!"


# 一个选项卡全局只能有一个音色
def get_vc(sid, to_return_protect0, to_return_protect1):
    global n_spk, tgt_sr, net_g, vc, cpt, version
    if sid == "" or sid == []:
        global hubert_model
        if hubert_model is not None:  # Considering polling, it is necessary to add a judgment to see if the sid is switched from a model to a modelless
            print("clean_empty_cache")
            del net_g, n_spk, vc, hubert_model, tgt_sr  # ,cpt
            hubert_model = net_g = n_spk = vc = hubert_model = tgt_sr = None
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            ###Downstairs is not so tossing and cleaning is not clean
            if_f0 = cpt.get("f0", 1)
            version = cpt.get("version", "v1")
            if version == "v1":
                if if_f0 == 1:
                    net_g = SynthesizerTrnMs256NSFsid(
                        *cpt["config"], is_half=config.is_half
                    )
                else:
                    net_g = SynthesizerTrnMs256NSFsid_nono(*cpt["config"])
            elif version == "v2":
                if if_f0 == 1:
                    net_g = SynthesizerTrnMs768NSFsid(
                        *cpt["config"], is_half=config.is_half
                    )
                else:
                    net_g = SynthesizerTrnMs768NSFsid_nono(*cpt["config"])
            del net_g, cpt
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            cpt = None
        return {"visible": False, "__type__": "update"}
    person = "%s/%s" % (weight_root, sid)
    print("loading %s" % person)
    cpt = torch.load(person, map_location="cpu")
    tgt_sr = cpt["config"][-1]
    cpt["config"][-3] = cpt["weight"]["emb_g.weight"].shape[0]  # n_spk
    if_f0 = cpt.get("f0", 1)
    if if_f0 == 0:
        to_return_protect0 = to_return_protect1 = {
            "visible": False,
            "value": 0.5,
            "__type__": "update",
        }
    else:
        to_return_protect0 = {
            "visible": True,
            "value": to_return_protect0,
            "__type__": "update",
        }
        to_return_protect1 = {
            "visible": True,
            "value": to_return_protect1,
            "__type__": "update",
        }
    version = cpt.get("version", "v1")
    if version == "v1":
        if if_f0 == 1:
            net_g = SynthesizerTrnMs256NSFsid(*cpt["config"], is_half=config.is_half)
        else:
            net_g = SynthesizerTrnMs256NSFsid_nono(*cpt["config"])
    elif version == "v2":
        if if_f0 == 1:
            net_g = SynthesizerTrnMs768NSFsid(*cpt["config"], is_half=config.is_half)
        else:
            net_g = SynthesizerTrnMs768NSFsid_nono(*cpt["config"])
    del net_g.enc_q
    print(net_g.load_state_dict(cpt["weight"], strict=False))
    net_g.eval().to(config.device)
    if config.is_half:
        net_g = net_g.half()
    else:
        net_g = net_g.float()
    vc = VC(tgt_sr, config)
    n_spk = cpt["config"][-3]
    return (
        {"visible": True, "maximum": n_spk, "__type__": "update"},
        to_return_protect0,
        to_return_protect1,
    )


def change_choices():
    names = []
    for name in os.listdir(weight_root):
        if name.endswith(".pth"):
            names.append(name)
    index_paths = []
    for root, dirs, files in os.walk(index_root, topdown=False):
        for name in files:
            if name.endswith(".index") and "trained" not in name:
                index_paths.append("%s/%s" % (root, name))
    return {"choices": sorted(names), "__type__": "update"}, {
        "choices": sorted(index_paths),
        "__type__": "update",
    }


def clean():
    return {"value": "", "__type__": "update"}


sr_dict = {
    "32k": 32000,
    "40k": 40000,
    "48k": 48000,
}


def if_done(done, p):
    while 1:
        if p.poll() is None:
            sleep(0.5)
        else:
            break
    done[0] = True


def if_done_multi(done, ps):
    while 1:
        # poll==None代表进程未结束
        # 只要有一个进程未结束都不停
        flag = 1
        for p in ps:
            if p.poll() is None:
                flag = 0
                sleep(0.5)
                break
        if flag == 1:
            break
    done[0] = True


def preprocess_dataset(trainset_dir, exp_dir, sr, n_p):
    sr = sr_dict[sr]
    os.makedirs("%s/logs/%s" % (now_dir, exp_dir), exist_ok=True)
    f = open("%s/logs/%s/preprocess.log" % (now_dir, exp_dir), "w")
    f.close()
    cmd = (
        config.python_cmd
        + " trainset_preprocess_pipeline_print.py %s %s %s %s/logs/%s "
        % (trainset_dir, sr, n_p, now_dir, exp_dir)
        + str(config.noparallel)
    )
    print(cmd)
    p = Popen(cmd, shell=True)  # , stdin=PIPE, stdout=PIPE,stderr=PIPE,cwd=now_dir
    ###煞笔gr, popen read都非得全跑完了再一次性读取, 不用gr就正常读一句输出一句;只能额外弄出一个文本流定时读
    done = [False]
    threading.Thread(
        target=if_done,
        args=(
            done,
            p,
        ),
    ).start()
    while 1:
        with open("%s/logs/%s/preprocess.log" % (now_dir, exp_dir), "r") as f:
            yield (f.read())
        sleep(1)
        if done[0]:
            break
    with open("%s/logs/%s/preprocess.log" % (now_dir, exp_dir), "r") as f:
        log = f.read()
    print(log)
    yield log


# but2.click(extract_f0,[gpus6,np7,f0method8,if_f0_3,trainset_dir4],[info2])
def extract_f0_feature(gpus, n_p, f0method, if_f0, exp_dir, version19, echl):
    gpus = gpus.split("-")
    os.makedirs("%s/logs/%s" % (now_dir, exp_dir), exist_ok=True)
    f = open("%s/logs/%s/extract_f0_feature.log" % (now_dir, exp_dir), "w")
    f.close()
    if if_f0:
        cmd = config.python_cmd + " extract_f0_print.py %s/logs/%s %s %s %s" % (
            now_dir,
            exp_dir,
            n_p,
            f0method,
            echl,
        )
        print(cmd)
        p = Popen(cmd, shell=True, cwd=now_dir)  # , stdin=PIPE, stdout=PIPE,stderr=PIPE
        ###煞笔gr, popen read都非得全跑完了再一次性读取, 不用gr就正常读一句输出一句;只能额外弄出一个文本流定时读
        done = [False]
        threading.Thread(
            target=if_done,
            args=(
                done,
                p,
            ),
        ).start()
        while 1:
            with open(
                "%s/logs/%s/extract_f0_feature.log" % (now_dir, exp_dir), "r"
            ) as f:
                yield (f.read())
            sleep(1)
            if done[0]:
                break
        with open("%s/logs/%s/extract_f0_feature.log" % (now_dir, exp_dir), "r") as f:
            log = f.read()
        print(log)
        yield log
    ####对不同part分别开多进程
    """
    n_part=int(sys.argv[1])
    i_part=int(sys.argv[2])
    i_gpu=sys.argv[3]
    exp_dir=sys.argv[4]
    os.environ["CUDA_VISIBLE_DEVICES"]=str(i_gpu)
    """
    leng = len(gpus)
    ps = []
    for idx, n_g in enumerate(gpus):
        cmd = (
            config.python_cmd
            + " extract_feature_print.py %s %s %s %s %s/logs/%s %s"
            % (
                config.device,
                leng,
                idx,
                n_g,
                now_dir,
                exp_dir,
                version19,
            )
        )
        print(cmd)
        p = Popen(
            cmd, shell=True, cwd=now_dir
        )  # , shell=True, stdin=PIPE, stdout=PIPE, stderr=PIPE, cwd=now_dir
        ps.append(p)
    ###煞笔gr, popen read都非得全跑完了再一次性读取, 不用gr就正常读一句输出一句;只能额外弄出一个文本流定时读
    done = [False]
    threading.Thread(
        target=if_done_multi,
        args=(
            done,
            ps,
        ),
    ).start()
    while 1:
        with open("%s/logs/%s/extract_f0_feature.log" % (now_dir, exp_dir), "r") as f:
            yield (f.read())
        sleep(1)
        if done[0]:
            break
    with open("%s/logs/%s/extract_f0_feature.log" % (now_dir, exp_dir), "r") as f:
        log = f.read()
    print(log)
    yield log


def change_sr2(sr2, if_f0_3, version19):
    path_str = "" if version19 == "v1" else "_v2"
    f0_str = "f0" if if_f0_3 else ""
    if_pretrained_generator_exist = os.access(
        "pretrained%s/%sG%s.pth" % (path_str, f0_str, sr2), os.F_OK
    )
    if_pretrained_discriminator_exist = os.access(
        "pretrained%s/%sD%s.pth" % (path_str, f0_str, sr2), os.F_OK
    )
    if not if_pretrained_generator_exist:
        print(
            "pretrained%s/%sG%s.pth" % (path_str, f0_str, sr2),
            "not exist, will not use pretrained model",
        )
    if not if_pretrained_discriminator_exist:
        print(
            "pretrained%s/%sD%s.pth" % (path_str, f0_str, sr2),
            "not exist, will not use pretrained model",
        )
    return (
        "pretrained%s/%sG%s.pth" % (path_str, f0_str, sr2)
        if if_pretrained_generator_exist
        else "",
        "pretrained%s/%sD%s.pth" % (path_str, f0_str, sr2)
        if if_pretrained_discriminator_exist
        else "",
    )


def change_version19(sr2, if_f0_3, version19):
    path_str = "" if version19 == "v1" else "_v2"
    if sr2 == "32k" and version19 == "v1":
        sr2 = "40k"
    to_return_sr2 = (
        {"choices": ["40k", "48k"], "__type__": "update", "value": sr2}
        if version19 == "v1"
        else {"choices": ["40k", "48k", "32k"], "__type__": "update", "value": sr2}
    )
    f0_str = "f0" if if_f0_3 else ""
    if_pretrained_generator_exist = os.access(
        "pretrained%s/%sG%s.pth" % (path_str, f0_str, sr2), os.F_OK
    )
    if_pretrained_discriminator_exist = os.access(
        "pretrained%s/%sD%s.pth" % (path_str, f0_str, sr2), os.F_OK
    )
    if not if_pretrained_generator_exist:
        print(
            "pretrained%s/%sG%s.pth" % (path_str, f0_str, sr2),
            "not exist, will not use pretrained model",
        )
    if not if_pretrained_discriminator_exist:
        print(
            "pretrained%s/%sD%s.pth" % (path_str, f0_str, sr2),
            "not exist, will not use pretrained model",
        )
    return (
        "pretrained%s/%sG%s.pth" % (path_str, f0_str, sr2)
        if if_pretrained_generator_exist
        else "",
        "pretrained%s/%sD%s.pth" % (path_str, f0_str, sr2)
        if if_pretrained_discriminator_exist
        else "",
        to_return_sr2,
    )


def change_f0(if_f0_3, sr2, version19):  # f0method8,pretrained_G14,pretrained_D15
    path_str = "" if version19 == "v1" else "_v2"
    if_pretrained_generator_exist = os.access(
        "pretrained%s/f0G%s.pth" % (path_str, sr2), os.F_OK
    )
    if_pretrained_discriminator_exist = os.access(
        "pretrained%s/f0D%s.pth" % (path_str, sr2), os.F_OK
    )
    if not if_pretrained_generator_exist:
        print(
            "pretrained%s/f0G%s.pth" % (path_str, sr2),
            "not exist, will not use pretrained model",
        )
    if not if_pretrained_discriminator_exist:
        print(
            "pretrained%s/f0D%s.pth" % (path_str, sr2),
            "not exist, will not use pretrained model",
        )
    if if_f0_3:
        return (
            {"visible": True, "__type__": "update"},
            "pretrained%s/f0G%s.pth" % (path_str, sr2)
            if if_pretrained_generator_exist
            else "",
            "pretrained%s/f0D%s.pth" % (path_str, sr2)
            if if_pretrained_discriminator_exist
            else "",
        )
    return (
        {"visible": False, "__type__": "update"},
        ("pretrained%s/G%s.pth" % (path_str, sr2))
        if if_pretrained_generator_exist
        else "",
        ("pretrained%s/D%s.pth" % (path_str, sr2))
        if if_pretrained_discriminator_exist
        else "",
    )


# but3.click(click_train,[exp_dir1,sr2,if_f0_3,save_epoch10,total_epoch11,batch_size12,if_save_latest13,pretrained_G14,pretrained_D15,gpus16])
def click_train(
    exp_dir1,
    sr2,
    if_f0_3,
    spk_id5,
    save_epoch10,
    total_epoch11,
    batch_size12,
    if_save_latest13,
    pretrained_G14,
    pretrained_D15,
    gpus16,
    if_cache_gpu17,
    if_save_every_weights18,
    version19,
):
    # 生成filelist
    exp_dir = "%s/logs/%s" % (now_dir, exp_dir1)
    os.makedirs(exp_dir, exist_ok=True)
    gt_wavs_dir = "%s/0_gt_wavs" % (exp_dir)
    feature_dir = (
        "%s/3_feature256" % (exp_dir)
        if version19 == "v1"
        else "%s/3_feature768" % (exp_dir)
    )
    if if_f0_3:
        f0_dir = "%s/2a_f0" % (exp_dir)
        f0nsf_dir = "%s/2b-f0nsf" % (exp_dir)
        names = (
            set([name.split(".")[0] for name in os.listdir(gt_wavs_dir)])
            & set([name.split(".")[0] for name in os.listdir(feature_dir)])
            & set([name.split(".")[0] for name in os.listdir(f0_dir)])
            & set([name.split(".")[0] for name in os.listdir(f0nsf_dir)])
        )
    else:
        names = set([name.split(".")[0] for name in os.listdir(gt_wavs_dir)]) & set(
            [name.split(".")[0] for name in os.listdir(feature_dir)]
        )
    opt = []
    for name in names:
        if if_f0_3:
            opt.append(
                "%s/%s.wav|%s/%s.npy|%s/%s.wav.npy|%s/%s.wav.npy|%s"
                % (
                    gt_wavs_dir.replace("\\", "\\\\"),
                    name,
                    feature_dir.replace("\\", "\\\\"),
                    name,
                    f0_dir.replace("\\", "\\\\"),
                    name,
                    f0nsf_dir.replace("\\", "\\\\"),
                    name,
                    spk_id5,
                )
            )
        else:
            opt.append(
                "%s/%s.wav|%s/%s.npy|%s"
                % (
                    gt_wavs_dir.replace("\\", "\\\\"),
                    name,
                    feature_dir.replace("\\", "\\\\"),
                    name,
                    spk_id5,
                )
            )
    fea_dim = 256 if version19 == "v1" else 768
    if if_f0_3:
        for _ in range(2):
            opt.append(
                "%s/logs/mute/0_gt_wavs/mute%s.wav|%s/logs/mute/3_feature%s/mute.npy|%s/logs/mute/2a_f0/mute.wav.npy|%s/logs/mute/2b-f0nsf/mute.wav.npy|%s"
                % (now_dir, sr2, now_dir, fea_dim, now_dir, now_dir, spk_id5)
            )
    else:
        for _ in range(2):
            opt.append(
                "%s/logs/mute/0_gt_wavs/mute%s.wav|%s/logs/mute/3_feature%s/mute.npy|%s"
                % (now_dir, sr2, now_dir, fea_dim, spk_id5)
            )
    shuffle(opt)
    with open("%s/filelist.txt" % exp_dir, "w") as f:
        f.write("\n".join(opt))
    print("write filelist done")
    # 生成config#无需生成config
    # cmd = python_cmd + " train_nsf_sim_cache_sid_load_pretrain.py -e mi-test -sr 40k -f0 1 -bs 4 -g 0 -te 10 -se 5 -pg pretrained/f0G40k.pth -pd pretrained/f0D40k.pth -l 1 -c 0"
    print("use gpus:", gpus16)
    if pretrained_G14 == "":
        print("no pretrained Generator")
    if pretrained_D15 == "":
        print("no pretrained Discriminator")
    if gpus16:
        cmd = (
            config.python_cmd
            + " train_nsf_sim_cache_sid_load_pretrain.py -e %s -sr %s -f0 %s -bs %s -g %s -te %s -se %s %s %s -l %s -c %s -sw %s -v %s"
            % (
                exp_dir1,
                sr2,
                1 if if_f0_3 else 0,
                batch_size12,
                gpus16,
                total_epoch11,
                save_epoch10,
                "-pg %s" % pretrained_G14 if pretrained_G14 != "" else "",
                "-pd %s" % pretrained_D15 if pretrained_D15 != "" else "",
                1 if if_save_latest13 == i18n("是") else 0,
                1 if if_cache_gpu17 == i18n("是") else 0,
                1 if if_save_every_weights18 == i18n("是") else 0,
                version19,
            )
        )
    else:
        cmd = (
            config.python_cmd
            + " train_nsf_sim_cache_sid_load_pretrain.py -e %s -sr %s -f0 %s -bs %s -te %s -se %s %s %s -l %s -c %s -sw %s -v %s"
            % (
                exp_dir1,
                sr2,
                1 if if_f0_3 else 0,
                batch_size12,
                total_epoch11,
                save_epoch10,
                "-pg %s" % pretrained_G14 if pretrained_G14 != "" else "\b",
                "-pd %s" % pretrained_D15 if pretrained_D15 != "" else "\b",
                1 if if_save_latest13 == i18n("是") else 0,
                1 if if_cache_gpu17 == i18n("是") else 0,
                1 if if_save_every_weights18 == i18n("是") else 0,
                version19,
            )
        )
    print(cmd)
    p = Popen(cmd, shell=True, cwd=now_dir)
    p.wait()
    return "Training Done تم الأنتهاء من التدريب"


# but4.click(train_index, [exp_dir1], info3)
def train_index(exp_dir1, version19):
    exp_dir = "%s/logs/%s" % (now_dir, exp_dir1)
    os.makedirs(exp_dir, exist_ok=True)
    feature_dir = (
        "%s/3_feature256" % (exp_dir)
        if version19 == "v1"
        else "%s/3_feature768" % (exp_dir)
    )
    if not os.path.exists(feature_dir):
        return "请先进行特征提取!"
    listdir_res = list(os.listdir(feature_dir))
    if len(listdir_res) == 0:
        return "请先进行特征提取！"
    infos = []
    npys = []
    for name in sorted(listdir_res):
        phone = np.load("%s/%s" % (feature_dir, name))
        npys.append(phone)
    big_npy = np.concatenate(npys, 0)
    big_npy_idx = np.arange(big_npy.shape[0])
    np.random.shuffle(big_npy_idx)
    big_npy = big_npy[big_npy_idx]
    if big_npy.shape[0] > 2e5:
        # if(1):
        infos.append("Trying doing kmeans %s shape to 10k centers." % big_npy.shape[0])
        yield "\n".join(infos)
        try:
            big_npy = (
                MiniBatchKMeans(
                    n_clusters=10000,
                    verbose=True,
                    batch_size=256 * config.n_cpu,
                    compute_labels=False,
                    init="random",
                )
                .fit(big_npy)
                .cluster_centers_
            )
        except:
            info = traceback.format_exc()
            print(info)
            infos.append(info)
            yield "\n".join(infos)

    np.save("%s/total_fea.npy" % exp_dir, big_npy)
    n_ivf = min(int(16 * np.sqrt(big_npy.shape[0])), big_npy.shape[0] // 39)
    infos.append("%s,%s" % (big_npy.shape, n_ivf))
    yield "\n".join(infos)
    index = faiss.index_factory(256 if version19 == "v1" else 768, "IVF%s,Flat" % n_ivf)
    # index = faiss.index_factory(256if version19=="v1"else 768, "IVF%s,PQ128x4fs,RFlat"%n_ivf)
    infos.append("training")
    yield "\n".join(infos)
    index_ivf = faiss.extract_index_ivf(index)  #
    index_ivf.nprobe = 1
    index.train(big_npy)
    faiss.write_index(
        index,
        "%s/trained_IVF%s_Flat_nprobe_%s_%s_%s.index"
        % (exp_dir, n_ivf, index_ivf.nprobe, exp_dir1, version19),
    )
    # faiss.write_index(index, '%s/trained_IVF%s_Flat_FastScan_%s.index'%(exp_dir,n_ivf,version19))
    infos.append("adding")
    yield "\n".join(infos)
    batch_size_add = 8192
    for i in range(0, big_npy.shape[0], batch_size_add):
        index.add(big_npy[i : i + batch_size_add])
    faiss.write_index(
        index,
        "%s/added_IVF%s_Flat_nprobe_%s_%s_%s.index"
        % (exp_dir, n_ivf, index_ivf.nprobe, exp_dir1, version19),
    )
    infos.append(
        "Successfully built index，added_IVF%s_Flat_nprobe_%s_%s_%s.index"
        % (n_ivf, index_ivf.nprobe, exp_dir1, version19)
    )
    # faiss.write_index(index, '%s/added_IVF%s_Flat_FastScan_%s.index'%(exp_dir,n_ivf,version19))
    # infos.append("成功构建索引，added_IVF%s_Flat_FastScan_%s.index"%(n_ivf,version19))
    yield "\n".join(infos)


# but5.click(train1key, [exp_dir1, sr2, if_f0_3, trainset_dir4, spk_id5, gpus6, np7, f0method8, save_epoch10, total_epoch11, batch_size12, if_save_latest13, pretrained_G14, pretrained_D15, gpus16, if_cache_gpu17], info3)
def train1key(
    exp_dir1,
    sr2,
    if_f0_3,
    trainset_dir4,
    spk_id5,
    np7,
    f0method8,
    save_epoch10,
    total_epoch11,
    batch_size12,
    if_save_latest13,
    pretrained_G14,
    pretrained_D15,
    gpus16,
    if_cache_gpu17,
    if_save_every_weights18,
    version19,
    echl
):
    infos = []

    def get_info_str(strr):
        infos.append(strr)
        return "\n".join(infos)

    model_log_dir = "%s/logs/%s" % (now_dir, exp_dir1)
    preprocess_log_path = "%s/preprocess.log" % model_log_dir
    extract_f0_feature_log_path = "%s/extract_f0_feature.log" % model_log_dir
    gt_wavs_dir = "%s/0_gt_wavs" % model_log_dir
    feature_dir = (
        "%s/3_feature256" % model_log_dir
        if version19 == "v1"
        else "%s/3_feature768" % model_log_dir
    )

    os.makedirs(model_log_dir, exist_ok=True)
    #########step1:处理数据
    open(preprocess_log_path, "w").close()
    cmd = (
        config.python_cmd
        + " trainset_preprocess_pipeline_print.py %s %s %s %s "
        % (trainset_dir4, sr_dict[sr2], np7, model_log_dir)
        + str(config.noparallel)
    )
    yield get_info_str(i18n("step1:正在处理数据"))
    yield get_info_str(cmd)
    p = Popen(cmd, shell=True)
    p.wait()
    with open(preprocess_log_path, "r") as f:
        print(f.read())
    #########step2a:提取音高
    open(extract_f0_feature_log_path, "w")
    if if_f0_3:
        yield get_info_str("step2a:正在提取音高")
        cmd = config.python_cmd + " extract_f0_print.py %s %s %s %s" % (
            model_log_dir,
            np7,
            f0method8,
            echl
        )
        yield get_info_str(cmd)
        p = Popen(cmd, shell=True, cwd=now_dir)
        p.wait()
        with open(extract_f0_feature_log_path, "r") as f:
            print(f.read())
    else:
        yield get_info_str(i18n("step2a:无需提取音高"))
    #######step2b:提取特征
    yield get_info_str(i18n("step2b:正在提取特征"))
    gpus = gpus16.split("-")
    leng = len(gpus)
    ps = []
    for idx, n_g in enumerate(gpus):
        cmd = config.python_cmd + " extract_feature_print.py %s %s %s %s %s %s" % (
            config.device,
            leng,
            idx,
            n_g,
            model_log_dir,
            version19,
        )
        yield get_info_str(cmd)
        p = Popen(
            cmd, shell=True, cwd=now_dir
        )  # , shell=True, stdin=PIPE, stdout=PIPE, stderr=PIPE, cwd=now_dir
        ps.append(p)
    for p in ps:
        p.wait()
    with open(extract_f0_feature_log_path, "r") as f:
        print(f.read())
    #######step3a:训练模型
    yield get_info_str(i18n("step3a:正在训练模型"))
    # 生成filelist
    if if_f0_3:
        f0_dir = "%s/2a_f0" % model_log_dir
        f0nsf_dir = "%s/2b-f0nsf" % model_log_dir
        names = (
            set([name.split(".")[0] for name in os.listdir(gt_wavs_dir)])
            & set([name.split(".")[0] for name in os.listdir(feature_dir)])
            & set([name.split(".")[0] for name in os.listdir(f0_dir)])
            & set([name.split(".")[0] for name in os.listdir(f0nsf_dir)])
        )
    else:
        names = set([name.split(".")[0] for name in os.listdir(gt_wavs_dir)]) & set(
            [name.split(".")[0] for name in os.listdir(feature_dir)]
        )
    opt = []
    for name in names:
        if if_f0_3:
            opt.append(
                "%s/%s.wav|%s/%s.npy|%s/%s.wav.npy|%s/%s.wav.npy|%s"
                % (
                    gt_wavs_dir.replace("\\", "\\\\"),
                    name,
                    feature_dir.replace("\\", "\\\\"),
                    name,
                    f0_dir.replace("\\", "\\\\"),
                    name,
                    f0nsf_dir.replace("\\", "\\\\"),
                    name,
                    spk_id5,
                )
            )
        else:
            opt.append(
                "%s/%s.wav|%s/%s.npy|%s"
                % (
                    gt_wavs_dir.replace("\\", "\\\\"),
                    name,
                    feature_dir.replace("\\", "\\\\"),
                    name,
                    spk_id5,
                )
            )
    fea_dim = 256 if version19 == "v1" else 768
    if if_f0_3:
        for _ in range(2):
            opt.append(
                "%s/logs/mute/0_gt_wavs/mute%s.wav|%s/logs/mute/3_feature%s/mute.npy|%s/logs/mute/2a_f0/mute.wav.npy|%s/logs/mute/2b-f0nsf/mute.wav.npy|%s"
                % (now_dir, sr2, now_dir, fea_dim, now_dir, now_dir, spk_id5)
            )
    else:
        for _ in range(2):
            opt.append(
                "%s/logs/mute/0_gt_wavs/mute%s.wav|%s/logs/mute/3_feature%s/mute.npy|%s"
                % (now_dir, sr2, now_dir, fea_dim, spk_id5)
            )
    shuffle(opt)
    with open("%s/filelist.txt" % model_log_dir, "w") as f:
        f.write("\n".join(opt))
    yield get_info_str("write filelist done")
    if gpus16:
        cmd = (
            config.python_cmd
            + " train_nsf_sim_cache_sid_load_pretrain.py -e %s -sr %s -f0 %s -bs %s -g %s -te %s -se %s %s %s -l %s -c %s -sw %s -v %s"
            % (
                exp_dir1,
                sr2,
                1 if if_f0_3 else 0,
                batch_size12,
                gpus16,
                total_epoch11,
                save_epoch10,
                "-pg %s" % pretrained_G14 if pretrained_G14 != "" else "",
                "-pd %s" % pretrained_D15 if pretrained_D15 != "" else "",
                1 if if_save_latest13 == i18n("是") else 0,
                1 if if_cache_gpu17 == i18n("是") else 0,
                1 if if_save_every_weights18 == i18n("是") else 0,
                version19,
            )
        )
    else:
        cmd = (
            config.python_cmd
            + " train_nsf_sim_cache_sid_load_pretrain.py -e %s -sr %s -f0 %s -bs %s -te %s -se %s %s %s -l %s -c %s -sw %s -v %s"
            % (
                exp_dir1,
                sr2,
                1 if if_f0_3 else 0,
                batch_size12,
                total_epoch11,
                save_epoch10,
                "-pg %s" % pretrained_G14 if pretrained_G14 != "" else "",
                "-pd %s" % pretrained_D15 if pretrained_D15 != "" else "",
                1 if if_save_latest13 == i18n("是") else 0,
                1 if if_cache_gpu17 == i18n("是") else 0,
                1 if if_save_every_weights18 == i18n("是") else 0,
                version19,
            )
        )
    yield get_info_str(cmd)
    p = Popen(cmd, shell=True, cwd=now_dir)
    p.wait()
    yield get_info_str(i18n("训练结束, 您可查看控制台训练日志或实验文件夹下的train.log"))
    #######step3b:训练索引
    npys = []
    listdir_res = list(os.listdir(feature_dir))
    for name in sorted(listdir_res):
        phone = np.load("%s/%s" % (feature_dir, name))
        npys.append(phone)
    big_npy = np.concatenate(npys, 0)

    big_npy_idx = np.arange(big_npy.shape[0])
    np.random.shuffle(big_npy_idx)
    big_npy = big_npy[big_npy_idx]

    if big_npy.shape[0] > 2e5:
        # if(1):
        info = "Trying doing kmeans %s shape to 10k centers." % big_npy.shape[0]
        print(info)
        yield get_info_str(info)
        try:
            big_npy = (
                MiniBatchKMeans(
                    n_clusters=10000,
                    verbose=True,
                    batch_size=256 * config.n_cpu,
                    compute_labels=False,
                    init="random",
                )
                .fit(big_npy)
                .cluster_centers_
            )
        except:
            info = traceback.format_exc()
            print(info)
            yield get_info_str(info)

    np.save("%s/total_fea.npy" % model_log_dir, big_npy)

    # n_ivf =  big_npy.shape[0] // 39
    n_ivf = min(int(16 * np.sqrt(big_npy.shape[0])), big_npy.shape[0] // 39)
    yield get_info_str("%s,%s" % (big_npy.shape, n_ivf))
    index = faiss.index_factory(256 if version19 == "v1" else 768, "IVF%s,Flat" % n_ivf)
    yield get_info_str("training index")
    index_ivf = faiss.extract_index_ivf(index)  #
    index_ivf.nprobe = 1
    index.train(big_npy)
    faiss.write_index(
        index,
        "%s/trained_IVF%s_Flat_nprobe_%s_%s_%s.index"
        % (model_log_dir, n_ivf, index_ivf.nprobe, exp_dir1, version19),
    )
    yield get_info_str("adding index")
    batch_size_add = 8192
    for i in range(0, big_npy.shape[0], batch_size_add):
        index.add(big_npy[i : i + batch_size_add])
    faiss.write_index(
        index,
        "%s/added_IVF%s_Flat_nprobe_%s_%s_%s.index"
        % (model_log_dir, n_ivf, index_ivf.nprobe, exp_dir1, version19),
    )
    yield get_info_str(
        "成功构建索引, added_IVF%s_Flat_nprobe_%s_%s_%s.index"
        % (n_ivf, index_ivf.nprobe, exp_dir1, version19)
    )
    yield get_info_str(i18n("全流程结束！"))


#                    ckpt_path2.change(change_info_,[ckpt_path2],[sr__,if_f0__])
def change_info_(ckpt_path):
    if not os.path.exists(ckpt_path.replace(os.path.basename(ckpt_path), "train.log")):
        return {"__type__": "update"}, {"__type__": "update"}, {"__type__": "update"}
    try:
        with open(
            ckpt_path.replace(os.path.basename(ckpt_path), "train.log"), "r"
        ) as f:
            info = eval(f.read().strip("\n").split("\n")[0].split("\t")[-1])
            sr, f0 = info["sample_rate"], info["if_f0"]
            version = "v2" if ("version" in info and info["version"] == "v2") else "v1"
            return sr, str(f0), version
    except:
        traceback.print_exc()
        return {"__type__": "update"}, {"__type__": "update"}, {"__type__": "update"}


def export_onnx(ModelPath, ExportedPath):
    cpt = torch.load(ModelPath, map_location="cpu")
    cpt["config"][-3] = cpt["weight"]["emb_g.weight"].shape[0]
    vec_channels = 256 if cpt.get("version", "v1") == "v1" else 768

    test_phone = torch.rand(1, 200, vec_channels)  # hidden unit
    test_phone_lengths = torch.tensor([200]).long()  # hidden unit 长度（貌似没啥用）
    test_pitch = torch.randint(size=(1, 200), low=5, high=255)  # 基频（单位赫兹）
    test_pitchf = torch.rand(1, 200)  # nsf基频
    test_ds = torch.LongTensor([0])  # 说话人ID
    test_rnd = torch.rand(1, 192, 200)  # 噪声（加入随机因子）

    device = "cpu"  # 导出时设备（不影响使用模型）


    net_g = SynthesizerTrnMsNSFsidM(
        *cpt["config"], is_half=False, version=cpt.get("version", "v1")
    )  # fp32导出（C++要支持fp16必须手动将内存重新排列所以暂时不用fp16）
    net_g.load_state_dict(cpt["weight"], strict=False)
    input_names = ["phone", "phone_lengths", "pitch", "pitchf", "ds", "rnd"]
    output_names = [
        "audio",
    ]
    # net_g.construct_spkmixmap(n_speaker) 多角色混合轨道导出
    torch.onnx.export(
        net_g,
        (
            test_phone.to(device),
            test_phone_lengths.to(device),
            test_pitch.to(device),
            test_pitchf.to(device),
            test_ds.to(device),
            test_rnd.to(device),
        ),
        ExportedPath,
        dynamic_axes={
            "phone": [1],
            "pitch": [1],
            "pitchf": [1],
            "rnd": [2],
        },
        do_constant_folding=False,
        opset_version=13,
        verbose=False,
        input_names=input_names,
        output_names=output_names,
    )
    return "Finished"


#region Mangio-RVC-Fork CLI App
import re as regex
import scipy.io.wavfile as wavfile

cli_current_page = "HOME"

def cli_split_command(com):
    exp = r'(?:(?<=\s)|^)"(.*?)"(?=\s|$)|(\S+)'
    split_array = regex.findall(exp, com)
    split_array = [group[0] if group[0] else group[1] for group in split_array]
    return split_array

def execute_generator_function(genObject):
    for _ in genObject: pass

def cli_infer(com):
    # get VC first
    com = cli_split_command(com)
    model_name = com[0]
    source_audio_path = com[1]
    output_file_name = com[2]
    feature_index_path = com[3]
    f0_file = None # Not Implemented Yet

    # Get parameters for inference
    speaker_id = int(com[4])
    transposition = float(com[5])
    f0_method = com[6]
    crepe_hop_length = int(com[7])
    harvest_median_filter = int(com[8])
    resample = int(com[9])
    mix = float(com[10])
    feature_ratio = float(com[11])
    protection_amnt = float(com[12])

    print("Mangio-RVC-Fork Infer-CLI: Starting the inference...")
    vc_data = get_vc(model_name)
    print(vc_data)
    print("Mangio-RVC-Fork Infer-CLI: Performing inference...")
    conversion_data = vc_single(
        speaker_id,
        source_audio_path,
        transposition,
        # f0_file,
        f0_method,
        feature_index_path,
        feature_index_path,
        feature_ratio,
        harvest_median_filter,
        resample,
        mix,
        protection_amnt,
        crepe_hop_length,        
    )
    if "Success." in conversion_data[0]:
        print("Mangio-RVC-Fork Infer-CLI: Inference succeeded. Writing to %s/%s..." % ('audio-outputs', output_file_name))
        wavfile.write('%s/%s' % ('audio-outputs', output_file_name), conversion_data[1][0], conversion_data[1][1])
        print("Mangio-RVC-Fork Infer-CLI: Finished! Saved output to %s/%s" % ('audio-outputs', output_file_name))
    else:
        print("Mangio-RVC-Fork Infer-CLI: Inference failed. Here's the traceback: ")
        print(conversion_data[0])

def cli_pre_process(com):
    com = cli_split_command(com)
    model_name = com[0]
    trainset_directory = com[1]
    sample_rate = com[2]
    num_processes = int(com[3])

    print("Mangio-RVC-Fork Pre-process: Starting...")
    generator = preprocess_dataset(
        trainset_directory, 
        model_name, 
        sample_rate, 
        num_processes
    )
    execute_generator_function(generator)
    print("Mangio-RVC-Fork Pre-process: Finished")

def cli_extract_feature(com):
    com = cli_split_command(com)
    model_name = com[0]
    gpus = com[1]
    num_processes = int(com[2])
    has_pitch_guidance = True if (int(com[3]) == 1) else False
    f0_method = com[4]
    crepe_hop_length = int(com[5])
    version = com[6] # v1 or v2
    
    print("Mangio-RVC-CLI: Extract Feature Has Pitch: " + str(has_pitch_guidance))
    print("Mangio-RVC-CLI: Extract Feature Version: " + str(version))
    print("Mangio-RVC-Fork Feature Extraction: Starting...")
    generator = extract_f0_feature(
        gpus, 
        num_processes, 
        f0_method, 
        has_pitch_guidance, 
        model_name, 
        version, 
        crepe_hop_length
    )
    execute_generator_function(generator)
    print("Mangio-RVC-Fork Feature Extraction: Finished")

def cli_train(com):
    com = cli_split_command(com)
    model_name = com[0]
    sample_rate = com[1]
    has_pitch_guidance = True if (int(com[2]) == 1) else False
    speaker_id = int(com[3])
    save_epoch_iteration = int(com[4])
    total_epoch = int(com[5]) # 10000
    batch_size = int(com[6])
    gpu_card_slot_numbers = com[7]
    if_save_latest = i18n("是") if (int(com[8]) == 1) else i18n("否")
    if_cache_gpu = i18n("是") if (int(com[9]) == 1) else i18n("否")
    if_save_every_weight = i18n("是") if (int(com[10]) == 1) else i18n("否")
    version = com[11]

    pretrained_base = "pretrained/" if version == "v1" else "pretrained_v2/" 
    
    g_pretrained_path = "%sf0G%s.pth" % (pretrained_base, sample_rate)
    d_pretrained_path = "%sf0D%s.pth" % (pretrained_base, sample_rate)

    print("Mangio-RVC-Fork Train-CLI: Training...")
    click_train(
        model_name,
        sample_rate,
        has_pitch_guidance,
        speaker_id,
        save_epoch_iteration,
        total_epoch,
        batch_size,
        if_save_latest,
        g_pretrained_path,
        d_pretrained_path,
        gpu_card_slot_numbers,
        if_cache_gpu,
        if_save_every_weight,
        version
    )

def cli_train_feature(com):
    com = cli_split_command(com)
    model_name = com[0]
    version = com[1]
    print("Mangio-RVC-Fork Train Feature Index-CLI: Training... Please wait")
    generator = train_index(
        model_name,
        version
    )
    execute_generator_function(generator)
    print("Mangio-RVC-Fork Train Feature Index-CLI: Done!")

def cli_extract_model(com):
    com = cli_split_command(com)
    model_path = com[0]
    save_name = com[1]
    sample_rate = com[2]
    has_pitch_guidance = com[3]
    info = com[4]
    version = com[5]
    extract_small_model_process = extract_small_model(
        model_path,
        save_name,
        sample_rate,
        has_pitch_guidance,
        info,
        version
    )
    if extract_small_model_process == "Success.":
        print("Mangio-RVC-Fork Extract Small Model: Success!")
    else:
        print(str(extract_small_model_process))        
        print("Mangio-RVC-Fork Extract Small Model: Failed!")

def print_page_details():
    if cli_current_page == "HOME":
        print("    go home            : Takes you back to home with a navigation list.")
        print("    go infer           : Takes you to inference command execution.\n")
        print("    go pre-process     : Takes you to training step.1) pre-process command execution.")
        print("    go extract-feature : Takes you to training step.2) extract-feature command execution.")
        print("    go train           : Takes you to training step.3) being or continue training command execution.")
        print("    go train-feature   : Takes you to the train feature index command execution.\n")
        print("    go extract-model   : Takes you to the extract small model command execution.")
    elif cli_current_page == "INFER":
        print("    arg 1) model name with .pth in ./weights: mi-test.pth")
        print("    arg 2) source audio path: myFolder\\MySource.wav")
        print("    arg 3) output file name to be placed in './audio-outputs': MyTest.wav")
        print("    arg 4) feature index file path: logs/mi-test/added_IVF3042_Flat_nprobe_1.index")
        print("    arg 5) speaker id: 0")
        print("    arg 6) transposition: 0")
        print("    arg 7) f0 method: harvest (pm, harvest, crepe, crepe-tiny, hybrid[x,x,x,x], mangio-crepe, mangio-crepe-tiny)")
        print("    arg 8) crepe hop length: 160")
        print("    arg 9) harvest median filter radius: 3 (0-7)")
        print("    arg 10) post resample rate: 0")
        print("    arg 11) mix volume envelope: 1")
        print("    arg 12) feature index ratio: 0.78 (0-1)")
        print("    arg 13) Voiceless Consonant Protection (Less Artifact): 0.33 (Smaller number = more protection. 0.50 means Dont Use.) \n")
        print("Example: mi-test.pth saudio/Sidney.wav myTest.wav logs/mi-test/added_index.index 0 -2 harvest 160 3 0 1 0.95 0.33")
    elif cli_current_page == "PRE-PROCESS":
        print("    arg 1) Model folder name in ./logs: mi-test")
        print("    arg 2) Trainset directory: mydataset (or) E:\\my-data-set")
        print("    arg 3) Sample rate: 40k (32k, 40k, 48k)")
        print("    arg 4) Number of CPU threads to use: 8 \n")
        print("Example: mi-test mydataset 40k 24")
    elif cli_current_page == "EXTRACT-FEATURE":
        print("    arg 1) Model folder name in ./logs: mi-test")
        print("    arg 2) Gpu card slot: 0 (0-1-2 if using 3 GPUs)")
        print("    arg 3) Number of CPU threads to use: 8")
        print("    arg 4) Has Pitch Guidance?: 1 (0 for no, 1 for yes)")
        print("    arg 5) f0 Method: harvest (pm, harvest, dio, crepe)")
        print("    arg 6) Crepe hop length: 128")
        print("    arg 7) Version for pre-trained models: v2 (use either v1 or v2)\n")
        print("Example: mi-test 0 24 1 harvest 128 v2")
    elif cli_current_page == "TRAIN":
        print("    arg 1) Model folder name in ./logs: mi-test")
        print("    arg 2) Sample rate: 40k (32k, 40k, 48k)")
        print("    arg 3) Has Pitch Guidance?: 1 (0 for no, 1 for yes)")
        print("    arg 4) speaker id: 0")
        print("    arg 5) Save epoch iteration: 50")
        print("    arg 6) Total epochs: 10000")
        print("    arg 7) Batch size: 8")
        print("    arg 8) Gpu card slot: 0 (0-1-2 if using 3 GPUs)")
        print("    arg 9) Save only the latest checkpoint: 0 (0 for no, 1 for yes)")
        print("    arg 10) Whether to cache training set to vram: 0 (0 for no, 1 for yes)")
        print("    arg 11) Save extracted small model every generation?: 0 (0 for no, 1 for yes)")
        print("    arg 12) Model architecture version: v2 (use either v1 or v2)\n")
        print("Example: mi-test 40k 1 0 50 10000 8 0 0 0 0 v2")
    elif cli_current_page == "TRAIN-FEATURE":
        print("    arg 1) Model folder name in ./logs: mi-test")
        print("    arg 2) Model architecture version: v2 (use either v1 or v2)\n")
        print("Example: mi-test v2")
    elif cli_current_page == "EXTRACT-MODEL":
        print("    arg 1) Model Path: logs/mi-test/G_168000.pth")
        print("    arg 2) Model save name: MyModel")
        print("    arg 3) Sample rate: 40k (32k, 40k, 48k)")
        print("    arg 4) Has Pitch Guidance?: 1 (0 for no, 1 for yes)")
        print('    arg 5) Model information: "My Model"')
        print("    arg 6) Model architecture version: v2 (use either v1 or v2)\n")
        print('Example: logs/mi-test/G_168000.pth MyModel 40k 1 "Created by Cole Mangio" v2')
    print("")

def change_page(page):
    global cli_current_page
    cli_current_page = page
    return 0

def execute_command(com):
    if com == "go home":
        return change_page("HOME")
    elif com == "go infer":
        return change_page("INFER")
    elif com == "go pre-process":
        return change_page("PRE-PROCESS")
    elif com == "go extract-feature":
        return change_page("EXTRACT-FEATURE")
    elif com == "go train":
        return change_page("TRAIN")
    elif com == "go train-feature":
        return change_page("TRAIN-FEATURE")
    elif com == "go extract-model":
        return change_page("EXTRACT-MODEL")
    else:
        if com[:3] == "go ":
            print("page '%s' does not exist!" % com[3:])
            return 0
    
    if cli_current_page == "INFER":
        cli_infer(com)
    elif cli_current_page == "PRE-PROCESS":
        cli_pre_process(com)
    elif cli_current_page == "EXTRACT-FEATURE":
        cli_extract_feature(com)
    elif cli_current_page == "TRAIN":
        cli_train(com)
    elif cli_current_page == "TRAIN-FEATURE":
        cli_train_feature(com)
    elif cli_current_page == "EXTRACT-MODEL":
        cli_extract_model(com)

def cli_navigation_loop():
    while True:
        print("You are currently in '%s':" % cli_current_page)
        print_page_details()
        command = input("%s: " % cli_current_page)
        try:
            execute_command(command)
        except:
            print(traceback.format_exc())

if(config.is_cli):
    print("\n\nMangio-RVC-Fork v2 CLI App!\n")
    print("Welcome to the CLI version of RVC. Please read the documentation on https://github.com/Mangio621/Mangio-RVC-Fork (README.MD) to understand how to use this app.\n")
    cli_navigation_loop()

#endregion

#region RVC WebUI App

def get_presets():
    data = None
    with open('../inference-presets.json', 'r') as file:
        data = json.load(file)
    preset_names = []
    for preset in data['presets']:
        preset_names.append(preset['name'])
    
    return preset_names

def save_wav(audioFile):
    if audioFile != None:
        file_path=audioFile.name
        shutil.copy2(file_path,'./audios')
        print(os.path.join('./audios',os.path.basename(file_path)))
        return os.path.join('./audios',os.path.basename(file_path))

def wav_change(audioFile):
    if audioFile != None:
        file_path=audioFile.name
        print(os.path.join('./audios',os.path.basename(file_path)))
        return os.path.join('./audios',os.path.basename(file_path))

def change_choices2():
    audio_files=[]
    for filename in os.listdir("./audios"):
        if filename.endswith(('.wav','.mp3')):
            audio_files.append(os.path.join('./audios',filename))
    return {"choices": sorted(audio_files), "__type__": "update"}, {"__type__": "update"}
    
audio_files=[]
for filename in os.listdir("./audios"):
    if filename.endswith(('.wav','.mp3')):
        audio_files.append(os.path.join('./audios',filename))

def get_name():
    if len(audio_files) > 0:
        return sorted(audio_files)[0]
    else:
        return ''
    
def zip_downloader(model):
    if not os.path.exists(f'./weights/{model}.pth'):
        return {"__type__": "update"}, f'Make sure the Model Name is correct. I could not find {model}.pth'
    index_found = False
    MODEL_EPOCH = 0
    all_model_files=[]
    for file in os.listdir(f'./logs/{model}'):
        if file.endswith('.index') and 'added' in file:
            log_file = file
            index_found = True
            index_url = f'./logs/{model}/{log_file}'
            model_url = f'./weights/{model}.pth'
            all_model_files.append(index_url)
            all_model_files.append(model_url)
        if file.endswith(".npy"):
            total_npy_url = f'./logs/{model}/total_fea.npy'
            all_model_files.append(total_npy_url)
        if file.startswith('G_') and '.pth' in file:
            g_file = file.split("_")
            g_file_1 = g_file[1].split(".")
            MODEL_EPOCH = g_file_1[0]
            g_file_url = f'./logs/{model}/G_{MODEL_EPOCH}.pth'
            all_model_files.append(g_file_url)
            d_file_url = f'./logs/{model}/D_{MODEL_EPOCH}.pth'
            all_model_files.append(d_file_url)
    if index_found:
        # return [f'./weights/{model}.pth', f'./logs/{model}/{log_file}', f'./logs/{model}/total_fea.npy', f'./logs/{model}/G_{MODEL_EPOCH}.pth', f'./logs/{model}/D_{MODEL_EPOCH}.pth'], "Done"
        return all_model_files, "Done"
    else:
        return f'./weights/{model}.pth', "Could not find Index file."
    
def zip_downloader_full(model):
    if not os.path.exists(f'./weights/{model}.pth'):
        return {"__type__": "update"}, f'Make sure the Model Name is correct. I could not find {model}.pth'
    model_dirs = model
    if os.path.exists(model_dirs):
        shutil.rmtree(model_dirs)
    os.makedirs(model, exist_ok=True)
    model_temp_path = f'./{model}/' + model_dirs
    try:
        shutil.copytree(f'./logs/{model}/', model_temp_path)
        shutil.copy(f'./weights/{model}.pth', f'./{model}/')
        shutil.make_archive(model, 'zip', f'./{model}/')
        return f'./{model}.zip', f'Success >>> {model} Model Ziped'
    except:
        return "There's been an error."

def audio_downloader():
    aud_files_list=[]
    if not os.path.exists(f'./opt'):
        return 'I could not find any audio file'
    for file in os.listdir('./opt'):
        if file.endswith('.wav') or file.endswith('.mp3'):
           aud_files_list.append(f'./opt/{file}')
    if len(aud_files_list) > 0:
        return aud_files_list, 'Found audio files'
    else:
        return aud_files_list, 'I could not find any audio file'

#TODO: Add Log To All Functions    
log = logging.getLogger("Mazen RVC [LOG] ====> ")

def download_from_url(url, model):
    if url == '':
        #log.info("Test")
        return "Google Drive File ID cannot be left empty."
    if model =='':
        return "You need to name your model. For example: Mazen-Model"
    url_full = f'https://drive.google.com/uc?id={url}'
    url = url_full
    url = url.strip()
    zip_dirs = ["zips", "unzips"]
    for directory in zip_dirs:
        if os.path.exists(directory):
            shutil.rmtree(directory)
    os.makedirs("zips", exist_ok=True)
    os.makedirs("unzips", exist_ok=True)
    zipfile = model + '.zip'
    zipfile_path = './zips/' + zipfile
    try:
        if "drive.google.com" in url:
            subprocess.run(["gdown", url, "--fuzzy", "-O", zipfile_path])
        elif "mega.nz" in url:
            m = Mega()
            m.download_url(url, './zips')
        else:
            subprocess.run(["wget", url, "-O", zipfile_path])
        for filename in os.listdir("./zips"):
            if filename.endswith(".zip"):
                zipfile_path = os.path.join("./zips/",filename)
                shutil.unpack_archive(zipfile_path, "./unzips", 'zip')
            else:
                return "No zipfile found."
        for root, dirs, files in os.walk('./unzips'):
            for file in files:
                print(files)
                file_path = os.path.join(root, file)
                if file.endswith(".index"):
                    os.makedirs(f'./logs/{model}', exist_ok=True)
                    shutil.copy(file_path,f'./logs/{model}')
                    if file.endswith(".npy"):
                        shutil.copy(file_path,f'./logs/{model}/total_fea.npy')
                if file.startswith('G_'):
                    g_file = file.split("_")
                    g_file_1 = g_file[1].split(".")
                    MODEL_EPOCH = g_file_1[0]
                    shutil.copy(file_path,f'./logs/{model}/G_{MODEL_EPOCH}.pth')
                    shutil.copy(file_path,f'./logs/{model}/D_{MODEL_EPOCH}.pth')
                elif "G_" not in file and "D_" not in file and file.endswith(".pth"):
                    shutil.copy(file_path,f'./weights/{model}.pth')
        shutil.rmtree("zips")
        shutil.rmtree("unzips")
        return "Success."
    except:
        return "There's been an error."
    
def download_model_full_from_url(url, model):
    if url == '':
        return "Google Drive File ID cannot be left empty."
    if model =='':
        return "You need to name your model. For example: Mazen-Model"
    url_full = f'https://drive.google.com/uc?id={url}'
    url = url_full
    url = url.strip()
    zip_dirs = ["model_zips", "model_unzips"]
    for directory in zip_dirs:
        if os.path.exists(directory):
            shutil.rmtree(directory)
    os.makedirs("model_zips", exist_ok=True)
    os.makedirs("model_unzips", exist_ok=True)
    zipfile = model + '.zip'
    zipfile_path = './model_zips/' + zipfile
    try:
        subprocess.run(["gdown", url, "--fuzzy", "-O", zipfile_path])
        for filename in os.listdir("./model_zips"):
            if filename.endswith(".zip"):
                zipfile_path = os.path.join("./model_zips/",filename)
                shutil.unpack_archive(zipfile_path, "./model_unzips", 'zip')
            else:
                return "No Model zipfile found."
        for root, dirs, files in os.walk('./model_unzips'):
            for file in files:
                if file.endswith('.pth') and model in file:
                    shutil.copy(f'./model_unzips/{model}.pth', f'./weights/{model}.pth')
            for dir in dirs:
                if dir == model:
                    try:
                        shutil.copytree(f'./model_unzips/{model}/', f'./logs/{model}')
                        shutil.rmtree("model_zips")
                        shutil.rmtree("model_unzips")
                        return f'Success >>> {model} Full Model'
                    except:
                        return "There's been an error. Code:Copy005"
    except:
        return "There's been an error."


def getAudioFilePath(file):
    return file
    
with gr.Blocks(theme=gr.themes.Soft(primary_hue="sky").set(
    button_primary_background_fill="*primary_100",
    button_primary_background_fill_hover="*primary_150"), title="Mazen RVC") as app:
    gr.HTML("<h1><center> Mazen RVC [Fork by Mazen VR Updated 10-7-2024] إستنساخ الصوت بالذكاء الإصطناعي </center></h1>")
    # gr.Markdown(
    #     value="<b>https://www.youtube.com/@MohamedElmazen</b>"
    # )
    # gr.Markdown(
    #     value="<b>Discord https://discord.gg/BXxagtuM38</b>"
    # )
    with gr.Tabs():
        with gr.TabItem(i18n("模型推理")):
            # Inference Preset Row
            # with gr.Row():
            #     mangio_preset = gr.Dropdown(label="Inference Preset", choices=sorted(get_presets()))
            #     mangio_preset_name_save = gr.Textbox(
            #         label="Your preset name"
            #     )
            #     mangio_preset_save_btn = gr.Button('Save Preset', variant="primary")

            # Other RVC stuff
            with gr.Row():
                sid0 = gr.Dropdown(label=i18n("推理音色"), choices=sorted(names))
                refresh_button = gr.Button(i18n("刷新音色列表和索引路径"), variant="primary")
                clean_button = gr.Button(i18n("卸载音色省显存"), variant="primary")
                spk_item = gr.Slider(
                    minimum=0,
                    maximum=2333,
                    step=1,
                    label=i18n("请选择说话人id"),
                    value=0,
                    visible=False,
                    interactive=True,
                )
                clean_button.click(fn=clean, inputs=[], outputs=[sid0])
            with gr.Group():
                gr.Markdown(
                    value=i18n("男转女推荐+12key, 女转男推荐-12key, 如果音域爆炸导致音色失真也可以自己调整到合适音域. ")
                )
                gr.Markdown(
                    value="([صوت رجل إلى صوت إنثى +12] [صوت إنثى إلى صوت رجل -12] [0 لنفس النوع] قم بظبط القيمة حتى تصل إلى أقرب نتيجة)"
                )
                with gr.Row():
                    with gr.Column():
                        vc_transform0 = gr.Number(
                            label=i18n("变调(整数, 半音数量, 升八度12降八度-12)"), value=0
                        )
                        input_audio0 = gr.Textbox(
                            label=i18n("输入待处理音频文件路径(默认是正确格式示例)"),
                            value="E:\\MazenWork\\py39\\test\\mazen.wav",
                        )
                        # input_audio1 = gr.Dropdown(
                        #     label="Choose your audio.",
                        #     choices=audio_files
                        #     )
                        dropbox_audio = gr.File(label="Drop your audio here", show_progress=True)
                        dropbox_audio.upload(fn=save_wav, inputs=dropbox_audio, outputs=input_audio0, show_progress=True)
                        dropbox_audio.change(fn=wav_change, inputs=dropbox_audio, outputs=input_audio0, show_progress=True)
                        #dropbox_audio.upload(fn=change_choices2, inputs=[], outputs=[input_audio0])
                        #input_audio1.change(fn=change_choices2, inputs=[], outputs=[input_audio0])
                        #refresh_audio_button = gr.Button("Refresh", variant="primary", size='sm')
                        #refresh_audio_button.click(fn=getAudioFilePath, inputs=[dropbox_audio], outputs=input_audio0)
                        f0method0 = gr.Radio(
                            label=i18n(
                                "选择音高提取算法,输入歌声可用pm提速,harvest低音好但巨慢无比,crepe效果好但吃GPU"
                            ),
                            choices=["pm", "harvest", "dio"], # Fork Feature. Add Crepe-Tiny
                            value="pm",
                            interactive=True,
                        )
                        crepe_hop_length = gr.Slider(
                            minimum=1,
                            maximum=512,
                            step=1,
                            label=i18n("crepe_hop_length"),
                            value=120,
                            interactive=True,
                            visible=False
                        )
                        filter_radius0 = gr.Slider(
                            minimum=0,
                            maximum=7,
                            label=i18n(">=3则使用对harvest音高识别的结果使用中值滤波，数值为滤波半径，使用可以削弱哑音"),
                            value=3,
                            step=1,
                            interactive=True,
                        )
                    with gr.Column():
                        file_index1 = gr.Textbox(
                            label=i18n("特征检索库文件路径,为空则使用下拉的选择结果"),
                            value="",
                            interactive=True,
                            visible=False
                        )
                        file_index2 = gr.Dropdown(
                            label=i18n("自动检测index路径,下拉式选择(dropdown)"),
                            choices=sorted(index_paths),
                            interactive=True,
                        )
                        refresh_button.click(
                            fn=change_choices, inputs=[], outputs=[sid0, file_index2]
                        )
                        # file_big_npy1 = gr.Textbox(
                        #     label=i18n("特征文件路径"),
                        #     value="E:\\codes\py39\\vits_vc_gpu_train\\logs\\mi-test-1key\\total_fea.npy",
                        #     interactive=True,
                        # )
                        index_rate1 = gr.Slider(
                            minimum=0,
                            maximum=1,
                            label=i18n("检索特征占比"),
                            value=0.60,
                            interactive=True,
                        )
                    with gr.Column():
                        resample_sr0 = gr.Slider(
                            minimum=0,
                            maximum=48000,
                            label=i18n("后处理重采样至最终采样率，0为不进行重采样"),
                            value=0,
                            step=1,
                            interactive=True,
                            visible=False
                        )
                        rms_mix_rate0 = gr.Slider(
                            minimum=0,
                            maximum=1,
                            label=i18n("输入源音量包络替换输出音量包络融合比例，越靠近1越使用输出包络"),
                            value=0.25,
                            interactive=True,
                            visible=False
                        )
                        protect0 = gr.Slider(
                            minimum=0,
                            maximum=0.5,
                            label=i18n(
                                "保护清辅音和呼吸声，防止电音撕裂等artifact，拉满0.5不开启，调低加大保护力度但可能降低索引效果"
                            ),
                            value=0.33,
                            step=0.01,
                            interactive=True,
                        )
                        # f0_file = gr.File(label=i18n("F0曲线文件, 可选, 一行一个音高, 代替默认F0及升降调"))
                        but0 = gr.Button(i18n("转换"), variant="primary")
                        with gr.Column():
                            vc_output1 = gr.Textbox(label=i18n("输出信息"))
                            vc_output2 = gr.Audio(label=i18n("输出音频(右下角三个点,点了可以下载)"))
                        but0.click(
                            vc_single,
                            [
                                spk_item,
                                input_audio0,
                                vc_transform0,
                                # f0_file,
                                f0method0,
                                file_index1,
                                file_index2,
                                # file_big_npy1,
                                index_rate1,
                                filter_radius0,
                                resample_sr0,
                                rms_mix_rate0,
                                protect0,
                                crepe_hop_length
                            ],
                            [vc_output1, vc_output2],
                        )
                        sid0.change(
                            fn=get_vc,
                            inputs=[sid0, protect0],
                            outputs=[spk_item, protect0],
                        )
            # with gr.Group():
            #     gr.Markdown(
            #         value=i18n("批量转换, 输入待转换音频文件夹, 或上传多个音频文件, 在指定文件夹(默认opt)下输出转换的音频. ")
            #     )
            #     with gr.Row():
            #         with gr.Column():
            #             vc_transform1 = gr.Number(
            #                 label=i18n("变调(整数, 半音数量, 升八度12降八度-12)"), value=0
            #             )
            #             opt_input = gr.Textbox(label=i18n("指定输出文件夹"), value="opt")
            #             f0method1 = gr.Radio(
            #                 label=i18n(
            #                     "选择音高提取算法,输入歌声可用pm提速,harvest低音好但巨慢无比,crepe效果好但吃GPU"
            #                 ),
            #                 choices=["pm", "harvest", "crepe", "rmvpe"],
            #                 value="pm",
            #                 interactive=True,
            #             )
            #             filter_radius1 = gr.Slider(
            #                 minimum=0,
            #                 maximum=7,
            #                 label=i18n(">=3则使用对harvest音高识别的结果使用中值滤波，数值为滤波半径，使用可以削弱哑音"),
            #                 value=3,
            #                 step=1,
            #                 interactive=True,
            #             )
            #         with gr.Column():
            #             file_index3 = gr.Textbox(
            #                 label=i18n("特征检索库文件路径,为空则使用下拉的选择结果"),
            #                 value="",
            #                 interactive=True,
            #             )
            #             file_index4 = gr.Dropdown(
            #                 label=i18n("自动检测index路径,下拉式选择(dropdown)"),
            #                 choices=sorted(index_paths),
            #                 interactive=True,
            #             )
            #             refresh_button.click(
            #                 fn=lambda: change_choices()[1],
            #                 inputs=[],
            #                 outputs=file_index4,
            #             )
            #             # file_big_npy2 = gr.Textbox(
            #             #     label=i18n("特征文件路径"),
            #             #     value="E:\\codes\\py39\\vits_vc_gpu_train\\logs\\mi-test-1key\\total_fea.npy",
            #             #     interactive=True,
            #             # )
            #             index_rate2 = gr.Slider(
            #                 minimum=0,
            #                 maximum=1,
            #                 label=i18n("检索特征占比"),
            #                 value=1,
            #                 interactive=True,
            #             )
            #         with gr.Column():
            #             resample_sr1 = gr.Slider(
            #                 minimum=0,
            #                 maximum=48000,
            #                 label=i18n("后处理重采样至最终采样率，0为不进行重采样"),
            #                 value=0,
            #                 step=1,
            #                 interactive=True,
            #             )
            #             rms_mix_rate1 = gr.Slider(
            #                 minimum=0,
            #                 maximum=1,
            #                 label=i18n("输入源音量包络替换输出音量包络融合比例，越靠近1越使用输出包络"),
            #                 value=1,
            #                 interactive=True,
            #             )
            #             protect1 = gr.Slider(
            #                 minimum=0,
            #                 maximum=0.5,
            #                 label=i18n(
            #                     "保护清辅音和呼吸声，防止电音撕裂等artifact，拉满0.5不开启，调低加大保护力度但可能降低索引效果"
            #                 ),
            #                 value=0.33,
            #                 step=0.01,
            #                 interactive=True,
            #             )
            #         with gr.Column():
            #             dir_input = gr.Textbox(
            #                 label=i18n("输入待处理音频文件夹路径(去文件管理器地址栏拷就行了)"),
            #                 value="E:\codes\py39\\test-20230416b\\todo-songs",
            #             )
            #             inputs = gr.File(
            #                 file_count="multiple", label=i18n("也可批量输入音频文件, 二选一, 优先读文件夹")
            #             )
            #         with gr.Row():
            #             format1 = gr.Radio(
            #                 label=i18n("导出文件格式"),
            #                 choices=["wav", "flac", "mp3", "m4a"],
            #                 value="flac",
            #                 interactive=True,
            #             )
            #             but1 = gr.Button(i18n("转换"), variant="primary")
            #             vc_output3 = gr.Textbox(label=i18n("输出信息"))
            #         but1.click(
            #             vc_multi,
            #             [
            #                 spk_item,
            #                 dir_input,
            #                 opt_input,
            #                 inputs,
            #                 vc_transform1,
            #                 f0method1,
            #                 file_index3,
            #                 file_index4,
            #                 # file_big_npy2,
            #                 index_rate2,
            #                 filter_radius1,
            #                 resample_sr1,
            #                 rms_mix_rate1,
            #                 protect1,
            #                 format1,
            #                 crepe_hop_length,
            #             ],
            #             [vc_output3],
            #         )
            # sid0.change(
            #     fn=get_vc,
            #     inputs=[sid0, protect0, protect1],
            #     outputs=[spk_item, protect0, protect1],
            # )

        with gr.TabItem(i18n("伴奏人声分离&去混响&去回声")):
            with gr.Group():
                with gr.Row():
                    with gr.Column():
                        dir_wav_input = gr.Textbox(
                            label="",
                            value="",
                            visible=True,
                            interactive=False
                        )
                        wav_inputs = gr.File(
                            file_count="multiple", label=i18n("也可批量输入音频文件, 二选一, 优先读文件夹"), show_progress=True
                        )
                        wav_inputs.upload(fn=save_wav_for_vocal, inputs=wav_inputs, outputs=dir_wav_input, show_progress=True)
                    with gr.Column():
                        model_choose = gr.Dropdown(label=i18n("模型"), choices=uvr5_names, value="HP5_only_main_vocal", visible=True)
                        agg = gr.Slider(
                            minimum=0,
                            maximum=20,
                            step=1,
                            label="人声提取激进程度",
                            value=10,
                            interactive=True,
                            visible=False,  # 先不开放调整
                        )
                        opt_vocal_root = gr.Textbox(
                            label=i18n("指定输出主人声文件夹"), value="opt",
                            visible=False
                        )
                        opt_ins_root = gr.Textbox(
                            label=i18n("指定输出非主人声文件夹"), value="opt",
                            visible=False
                        )
                        format0 = gr.Radio(
                            label=i18n("导出文件格式"),
                            choices=["wav", "mp3"],
                            value="wav",
                            interactive=True,
                            visible=False
                        )
                    but2 = gr.Button(i18n("转换"), variant="primary")
                    vc_output4 = gr.Textbox(label=i18n("输出信息"))
                    but2.click(
                        uvr,
                        [
                            model_choose,
                            dir_wav_input,
                            opt_vocal_root,
                            wav_inputs,
                            opt_ins_root,
                            agg,
                            format0,
                        ],
                        [vc_output4],
                    )
            with gr.Accordion("Download Audio Files تحميل ملفات الصوت ", open=True):
                with gr.Column():
                    aud_files = gr.Files(label='Audio files can be downloaded here: ملفات الصوت يمكن تحميلها من هنا')
                    status_bar_3=gr.Textbox(label="Log سجل النتائج")
                    aud_files_button = gr.Button('Download Audio Files تحميل ملفات الصوت', variant="primary")
                    aud_files_button.click(fn=audio_downloader, inputs=[], outputs=[aud_files, status_bar_3])   
       
        with gr.TabItem(i18n("训练")):
            gr.Markdown(
                value=i18n(
                    "step1: 填写实验配置. 实验数据放在logs下, 每个实验一个文件夹, 需手工输入实验名路径, 内含实验配置, 日志, 训练得到的模型文件. "
                )
            )
            with gr.Row():
                exp_dir1 = gr.Textbox(label=i18n("输入实验名"), value="MazenTest")
                sr2 = gr.Radio(
                    label=i18n("目标采样率"),
                    choices=["40k", "48k"],
                    value="40k",
                    interactive=True,
                    visible=False
                )
                if_f0_3 = gr.Radio(
                    label=i18n("模型是否带音高指导(唱歌一定要, 语音可以不要)"),
                    choices=[True, False],
                    value=True,
                    interactive=True,
                    visible=False
                )
                version19 = gr.Radio(
                    label=i18n("版本"),
                    choices=["v2"],
                    value="v2",
                    interactive=True,
                    visible=False,
                )
                np7 = gr.Slider(
                    minimum=0,
                    maximum=config.n_cpu,
                    step=1,
                    label=i18n("提取音高和处理数据使用的CPU进程数"),
                    value=int(np.ceil(config.n_cpu / 1.5)),
                    interactive=True,
                )
            with gr.Group():  # 暂时单人的, 后面支持最多4人的#数据处理
                gr.Markdown(
                    value=i18n(
                        "step2a: 自动遍历训练文件夹下所有可解码成音频的文件并进行切片归一化, 在实验目录下生成2个wav文件夹; 暂时只支持单人训练. "
                    )
                )
                with gr.Row():
                    trainset_dir4 = gr.Textbox(
                        label=i18n("输入训练文件夹路径"), value="/kaggle/working/dataset"
                    )
                    spk_id5 = gr.Slider(
                        minimum=0,
                        maximum=4,
                        step=1,
                        label=i18n("请指定说话人id"),
                        value=0,
                        interactive=True,
                        visible=True
                    )
                    but1 = gr.Button(i18n("处理数据"), variant="primary")
                    info1 = gr.Textbox(label=i18n("输出信息"), value="")
                    but1.click(
                        preprocess_dataset, [trainset_dir4, exp_dir1, sr2, np7], [info1]
                    )
            with gr.Group():
                gr.Markdown(value=i18n("step2b: 使用CPU提取音高(如果模型带音高), 使用GPU提取特征(选择卡号)"))
                with gr.Row():
                    with gr.Column():
                        gpus6 = gr.Textbox(
                            label=i18n("以-分隔输入使用的卡号, 例如   0-1-2   使用卡0和卡1和卡2"),
                            value=gpus,
                            interactive=True,
                        )
                        gpu_info9 = gr.Textbox(label=i18n("显卡信息"), value=gpu_info)
                    with gr.Column():
                        f0method8 = gr.Radio(
                            label=i18n(
                                "选择音高提取算法:输入歌声可用pm提速,高质量语音但CPU差可用dio提速,harvest质量更好但慢"
                            ),
                            choices=["pm", "harvest", "dio"], # Fork feature: Crepe on f0 extraction for training.
                            value="harvest",
                            interactive=True,
                        )
                        extraction_crepe_hop_length = gr.Slider(
                            minimum=1,
                            maximum=512,
                            step=1,
                            label=i18n("crepe_hop_length"),
                            value=64,
                            interactive=True,
                            visible=False
                        )
                    but2 = gr.Button(i18n("特征提取"), variant="primary")
                    info2 = gr.Textbox(label=i18n("输出信息"), value="", max_lines=8)
                    but2.click(
                        extract_f0_feature,
                        [gpus6, np7, f0method8, if_f0_3, exp_dir1, version19, extraction_crepe_hop_length],
                        [info2],
                    )
            with gr.Group():
                gr.Markdown(value=i18n("step3: 填写训练设置, 开始训练模型和索引"))
                with gr.Row():
                    save_epoch10 = gr.Slider(
                        minimum=0,
                        maximum=50,
                        step=1,
                        label=i18n("保存频率save_every_epoch"),
                        value=50,
                        interactive=True,
                    )
                    total_epoch11 = gr.Slider(
                        minimum=0,
                        maximum=10000,
                        step=1,
                        label=i18n("总训练轮数total_epoch"),
                        value=200,
                        interactive=True,
                    )
                    batch_size12 = gr.Slider(
                        minimum=1,
                        maximum=40,
                        step=1,
                        label=i18n("每张显卡的batch_size"),
                        value=default_batch_size,
                        interactive=True,
                    )
                    if_save_latest13 = gr.Radio(
                        label=i18n("是否仅保存最新的ckpt文件以节省硬盘空间"),
                        choices=[i18n("是"), i18n("否")],
                        value=i18n("是"),
                        interactive=True,
                    )
                    if_cache_gpu17 = gr.Radio(
                        label=i18n(
                            "是否缓存所有训练集至显存. 10min以下小数据可缓存以加速训练, 大数据缓存会炸显存也加不了多少速"
                        ),
                        choices=[i18n("是"), i18n("否")],
                        value=i18n("否"),
                        interactive=True,
                        visible=True
                    )
                    if_save_every_weights18 = gr.Radio(
                        label=i18n("是否在每次保存时间点将最终小模型保存至weights文件夹"),
                        choices=[i18n("是"), i18n("否")],
                        value=i18n("是"),
                        interactive=True,
                    )
                with gr.Row():
                    gr.Markdown("Step 4: Train Model first, and after that Train index features")
                with gr.Row():
                    pretrained_G14 = gr.Textbox(
                        label=i18n("加载预训练底模G路径"),
                        value="pretrained_v2/f0G40k.pth",
                        interactive=True,
                    )
                    pretrained_D15 = gr.Textbox(
                        label=i18n("加载预训练底模D路径"),
                        value="pretrained_v2/f0D40k.pth",
                        interactive=True,
                    )
                    sr2.change(
                        change_sr2,
                        [sr2, if_f0_3, version19],
                        [pretrained_G14, pretrained_D15],
                    )
                    version19.change(
                        change_version19,
                        [sr2, if_f0_3, version19],
                        [pretrained_G14, pretrained_D15, sr2],
                    )
                    if_f0_3.change(
                        change_f0,
                        [if_f0_3, sr2, version19],
                        [f0method8, pretrained_G14, pretrained_D15],
                    )
                    gpus16 = gr.Textbox(
                        label=i18n("以-分隔输入使用的卡号, 例如   0-1-2   使用卡0和卡1和卡2"),
                        value=gpus,
                        interactive=True,
                    )
                    but3 = gr.Button(i18n("训练模型"), variant="primary")
                    but4 = gr.Button(i18n("训练特征索引"), variant="primary")
                    but5 = gr.Button(i18n("一键训练"), variant="primary", visible=False)
                    info3 = gr.Textbox(label=i18n("输出信息"), value="", max_lines=10)
                    but3.click(
                        click_train,
                        [
                            exp_dir1,
                            sr2,
                            if_f0_3,
                            spk_id5,
                            save_epoch10,
                            total_epoch11,
                            batch_size12,
                            if_save_latest13,
                            pretrained_G14,
                            pretrained_D15,
                            gpus16,
                            if_cache_gpu17,
                            if_save_every_weights18,
                            version19,
                        ],
                        info3,
                    )
                    but4.click(train_index, [exp_dir1, version19], info3)
                    but5.click(
                        train1key,
                        [
                            exp_dir1,
                            sr2,
                            if_f0_3,
                            trainset_dir4,
                            spk_id5,
                            np7,
                            f0method8,
                            save_epoch10,
                            total_epoch11,
                            batch_size12,
                            if_save_latest13,
                            pretrained_G14,
                            pretrained_D15,
                            gpus16,
                            if_cache_gpu17,
                            if_save_every_weights18,
                            version19,
                            extraction_crepe_hop_length
                        ],
                        info3,
                    )
        with gr.TabItem("Model Manger مدير النماذج"):
            with gr.Accordion("Import Export Trained Model (For Inference Only)", open=False):
                with gr.Accordion("Import From Google Drive To Working Folder إستيراد نموذج لمجلد العمل", open=False):
                    with gr.Column():
                        gdrive_file_ID=gr.Textbox(label="Enter Google Drive File ID:")
                        model = gr.Textbox(label="Enter Model Name (Name must be as model saved name):")
                        status_bar_0=gr.Textbox(label="Log سجل النتائج")
                        download_button=gr.Button("Download Model From Google Drive",  variant="primary")
                        download_button.click(fn=download_from_url, inputs=[gdrive_file_ID, model], outputs=[status_bar_0])
                with gr.Accordion("Export Model تصدير النموذج ", open=False):
                    with gr.Column():
                        model_to_downlaod = gr.Textbox(label="Enter Model Name:")
                        zipped_model = gr.Files(label='Model files can be downloaded here:')
                        status_bar_1=gr.Textbox(label="Log سجل النتائج")
                        zip_model_button = gr.Button('Download Model From Working Folder', variant="primary")
                        zip_model_button.click(fn=zip_downloader, inputs=[model_to_downlaod], outputs=[zipped_model, status_bar_1])

            with gr.Accordion("Import Export Full Model (For Re-Train)", open=False):

                with gr.Accordion("Import Full Model From Google Drive To Working Folder", open=False):
                    with gr.Column():
                        gdrive_file_ID=gr.Textbox(label="Enter Google Drive File ID:")
                        model = gr.Textbox(label="Enter Model Name (Name must be as model saved name):")
                        status_bar_0=gr.Textbox(label="Log سجل النتائج")
                        download_button=gr.Button("Download Full Model From Google Drive",  variant="primary")
                        download_button.click(fn=download_model_full_from_url, inputs=[gdrive_file_ID, model], outputs=[status_bar_0])

                with gr.Accordion("Export Full Model", open=False):
                    with gr.Column():
                        model_name_to_downlaod = gr.Textbox(label="Enter Model Name:")
                        zipped_model.full = gr.File(label='Model .zip can be downloaded here:')
                        status_bar_2=gr.Textbox(label="Log سجل النتائج")
                        zip_model_full_button = gr.Button('Download ZIP Model From Working Folder', variant="primary")
                        zip_model_full_button.click(fn=zip_downloader_full, inputs=[model_name_to_downlaod], outputs=[zipped_model.full, status_bar_2])

            with gr.Accordion("Help مساعدة", open=False, visible=False):
                with gr.Column():
                    Help_bar_1=gr.Textbox(label="", value="Recommended to use MEGA for file uploading ننصح بإستخدام موقع ميجا لتحميل الملفات")
                    gr.Markdown(value="<b>https://mega.nz/</b>")
    
    gr.Markdown(
        value="<b>https://www.youtube.com/@MohamedElmazen</b>"
    )
    gr.Markdown(
        value="<b>Discord https://discord.gg/BXxagtuM38</b>"
    )

    #region Mangio Preset Handler Region
    def save_preset(
        preset_name,
        sid0,
        vc_transform,
        input_audio,
        f0method,
        crepe_hop_length,
        filter_radius,
        file_index1,
        file_index2,
        index_rate,
        resample_sr,
        rms_mix_rate,
        protect,
        # f0_file
    ):
        data = None
        with open('../inference-presets.json', 'r') as file:
            data = json.load(file)
        preset_json = {
            'name': preset_name,
            'model': sid0,
            'transpose': vc_transform,
            'audio_file': input_audio,
            'f0_method': f0method,
            'crepe_hop_length': crepe_hop_length,
            'median_filtering': filter_radius,
            'feature_path': file_index1,
            'auto_feature_path': file_index2,
            'search_feature_ratio': index_rate,
            'resample': resample_sr,
            'volume_envelope': rms_mix_rate,
            'protect_voiceless': protect,
            'f0_file_path': "f0_file"
        }
        data['presets'].append(preset_json)
        with open('../inference-presets.json', 'w') as file:
            json.dump(data, file)
            file.flush()
        print("Saved Preset %s into inference-presets.json!" % preset_name)


    def on_preset_changed(preset_name):
        print("Changed Preset to %s!" % preset_name)
        data = None
        with open('../inference-presets.json', 'r') as file:
            data = json.load(file)

        print("Searching for " + preset_name)
        returning_preset = None
        for preset in data['presets']:
            if(preset['name'] == preset_name):
                print("Found a preset")
                returning_preset = preset
        # return all new input values
        return (
            # returning_preset['model'],
            # returning_preset['transpose'],
            # returning_preset['audio_file'],
            # returning_preset['f0_method'],
            # returning_preset['crepe_hop_length'],
            # returning_preset['median_filtering'],
            # returning_preset['feature_path'],
            # returning_preset['auto_feature_path'],
            # returning_preset['search_feature_ratio'],
            # returning_preset['resample'],
            # returning_preset['volume_envelope'],
            # returning_preset['protect_voiceless'],
            # returning_preset['f0_file_path']
        )

    # Preset State Changes                
    
    # This click calls save_preset that saves the preset into inference-presets.json with the preset name
    # mangio_preset_save_btn.click(
    #     fn=save_preset, 
    #     inputs=[
    #         mangio_preset_name_save,
    #         sid0,
    #         vc_transform0,
    #         input_audio0,
    #         f0method0,
    #         crepe_hop_length,
    #         filter_radius0,
    #         file_index1,
    #         file_index2,
    #         index_rate1,
    #         resample_sr0,
    #         rms_mix_rate0,
    #         protect0,
    #         f0_file
    #     ], 
    #     outputs=[]
    # )

    # mangio_preset.change(
    #     on_preset_changed, 
    #     inputs=[
    #         # Pass inputs here
    #         mangio_preset
    #     ], 
    #     outputs=[
    #         # Pass Outputs here. These refer to the gradio elements that we want to directly change
    #         # sid0,
    #         # vc_transform0,
    #         # input_audio0,
    #         # f0method0,
    #         # crepe_hop_length,
    #         # filter_radius0,
    #         # file_index1,
    #         # file_index2,
    #         # index_rate1,
    #         # resample_sr0,
    #         # rms_mix_rate0,
    #         # protect0,
    #         # f0_file
    #     ]
    # )
    #endregion

        # with gr.TabItem(i18n("招募音高曲线前端编辑器")):
        #     gr.Markdown(value=i18n("加开发群联系我xxxxx"))
        # with gr.TabItem(i18n("点击查看交流、问题反馈群号")):
        #     gr.Markdown(value=i18n("xxxxx"))

    if config.iscolab or config.paperspace: # Share gradio link for colab and paperspace (FORK FEATURE)
        app.queue(concurrency_count=511, max_size=1022).launch(share=True)
    else:
        app.queue(concurrency_count=511, max_size=1022).launch(
            server_name="0.0.0.0",
            inbrowser=not config.noautoopen,
            server_port=config.listen_port,
            quiet=True,
        )

#endregion
