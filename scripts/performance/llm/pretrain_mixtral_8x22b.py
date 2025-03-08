# Copyright (c) 2025, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from os.path import basename, splitext
from typing import Optional

import nemo_run as run

from nemo.collections.llm.recipes.mixtral_8x22b import pretrain_recipe
from nemo.collections.llm.recipes.precision.mixed_precision import bf16_with_fp8_mixed
from nemo.lightning.run.plugins import NsysPlugin, PerfEnvPlugin

from ..argument_parser import parse_cli_args
from ..utils import args_sanity_check, get_user_configs, hf_tokenizer, set_primary_perf_configs, slurm_executor


def override_recipe_configs(
    args: str,
    num_nodes: int,
    mbs: int,
    gbs: int,
    tp_size: int,
    pp_size: int,
    cp_size: int,
    vp_size: int,
    ep_size: int,
    etp_size: Optional[int],
    enable_cuda_graphs: bool,
):
    """
    mixtral 8x22b pre-train recipe aimed at achieving best possible performance.

    NOTE: Use fp8 precision training with caution. It might not give desirable results.
    """
    recipe = pretrain_recipe(performance_mode=True)
    recipe = set_primary_perf_configs(
        recipe,
        args.tensorboard,
        args.wandb,
        args.wandb_prj_name,
        args.wandb_job_name,
        num_nodes,
        args.gpus_per_node,
        mbs,
        gbs,
        args.max_steps,
        tp_size,
        pp_size,
        cp_size,
        vp_size,
        ep_size,
        etp_size,
    )

    # data module configs
    recipe.data.num_train_samples = args.max_steps * gbs * mbs  # ensure only 1 epoch for whole run
    recipe.data.tokenizer = hf_tokenizer("mistralai/Mixtral-8x22B-v0.1")

    # compute dtype configs
    if args.compute_dtype.lower() == "fp8":
        recipe.trainer.plugins = bf16_with_fp8_mixed()
        recipe.trainer.plugins.grad_reduce_in_fp32 = False

    # to mitigate the incorrect gradient_scaling_factor calculation in megatron.core
    # under scenario average_in_collective=True and tp_size != etp_size, disabling average_in_collective.
    if etp_size is not None and etp_size != tp_size:
        recipe.trainer.strategy.ddp.average_in_collective = False

    recipe.model.config.enable_cuda_graph = enable_cuda_graphs
    recipe.trainer.strategy.use_te_rng_tracker = enable_cuda_graphs

    return recipe


if __name__ == "__main__":
    args = parse_cli_args().parse_args()
    args_sanity_check(args)

    kwargs = get_user_configs(args.gpu.lower(), "pre_train", "mixtral", "8x22b", args)
    num_nodes, mbs, gbs, tp_size, pp_size, cp_size, vp_size, ep_size, etp_size, _, enable_cuda_graphs = kwargs

    recipe = override_recipe_configs(
        args, num_nodes, mbs, gbs, tp_size, pp_size, cp_size, vp_size, ep_size, etp_size, enable_cuda_graphs
    )

    exp_config = (
        f"{num_nodes}nodes_tp{tp_size}_pp{pp_size}_cp{cp_size}_vp{vp_size}_ep{ep_size}_etp{etp_size}_{mbs}mbs_{gbs}gbs"
    )
    exp_name = f"{splitext(basename(__file__))[0]}_{args.compute_dtype}_{exp_config}"

    executor = slurm_executor(
        args.account,
        args.partition,
        args.log_dir,
        num_nodes,
        args.gpus_per_node,
        args.time_limit,
        args.container_image,
        custom_mounts=args.custom_mounts,
        custom_env_vars={},
        hf_token=args.hf_token,
        nemo_home=args.nemo_home,
        wandb_key=args.wandb_key,
    )

    plugins = [PerfEnvPlugin(enable_vboost=True, nccl_pp_comm_chunksize=2097152 if pp_size > 1 else None)]
    if args.enable_nsys:
        plugins.append(NsysPlugin(start_step=5, end_step=6))

    with run.Experiment(exp_name) as exp:
        exp.add(
            recipe,
            executor=executor,
            name=exp_name,
            plugins=plugins,
        )

        if not args.dryrun:
            exp.run(sequential=True, detach=True)
        else:
            exp.dryrun()
